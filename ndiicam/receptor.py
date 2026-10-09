#!/usr/bin/env python3
# NDIICAM — proceso del receptor: una fuente NDI de la red a la salida HDMI.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Recibe una fuente NDI y la muestra en el monitor, con su audio por HDMI.

    receptor.py '<json>'   con nombre (fuente NDI), hdmi (id del conector DRM),
                           audio (dispositivo ALSA de salida, o null) y equipo
                           (nombre con el que nos ve el emisor) y pantalla
                           ([ancho, alto] de la pantalla, o null)

Igual que fuente.py: cada 2 s escribe una línea JSON con los contadores y el
formato recibido; si el pipeline falla, escribe {"error": ..., "elemento": ...}
y termina con código 1. Que no lleguen imágenes no es un fallo: un emisor NDI
puede mandar una sola mientras no cambie. Si el emisor desaparece, ndisrc da
error por su cuenta.
"""
import json
import os
import signal
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

CONTADORES = {"rx": 0, "aud": 0}
FORMATO = {}


def informar(datos):
    sys.stdout.write(json.dumps(datos) + "\n")
    sys.stdout.flush()


def main():
    cfg = json.loads(sys.argv[1])
    # una fuente que obliga a escalar puede pedir más CPU de la que hay: con
    # prioridad baja y un núcleo libre, el sistema, la red y las cámaras van
    # antes (con el equipo saturado minutos seguidos, un Atom se llegó a colgar)
    os.nice(10)
    nucleos = sorted(os.sched_getaffinity(0))
    if len(nucleos) >= 3:
        os.sched_setaffinity(0, nucleos[1:])
    Gst.init(None)
    # NDI entrega UYVY, que va tal cual a un plano de vídeo de la GPU: mostrarlo
    # no cuesta CPU. Sin force-modesetting (con él kmssink solo acepta RGB); la
    # pantalla ya está a la resolución pedida en el arranque (video=). Con
    # skip-vsync, porque si no kmssink espera dos refrescos por imagen y no pasa
    # de 30 fps en una pantalla de 60 Hz. Los dos sumideros con async=false: si
    # la fuente no trae audio (o vídeo), el que no recibe nada dejaría el
    # pipeline esperando en pausa
    # Los planos de vídeo no escalan (Cherry Trail no): una fuente de otra
    # resolución se escala por CPU, con bordes para mantener la proporción y por
    # el vecino más próximo, que cuesta mucho menos que el bilineal. Si ya
    # coincide con la pantalla, videoscale y videoconvert no tocan nada
    hilos = len(os.sched_getaffinity(0))
    escala = ""
    if cfg.get("pantalla"):
        ancho, alto = cfg["pantalla"]
        escala = (f" ! videoscale n-threads={hilos} add-borders=true method=nearest-neighbour"
                  f" ! videoconvert n-threads={hilos}"
                  f" ! video/x-raw,width={ancho},height={alto},pixel-aspect-ratio=1/1")
    desc = ("ndisrc name=src ! ndisrcdemux name=d "
            "d.video ! queue name=qv max-size-buffers=2 leaky=downstream" + escala +
            " ! kmssink name=hdmi sync=false async=false skip-vsync=true"
            f" connector-id={cfg['hdmi']}")
    # el audio se enlaza siempre: si la fuente lo trae y su salida no está
    # enlazada, el demultiplexor tumba el pipeline
    desc += " d.audio ! queue name=qa max-size-time=300000000 leaky=downstream"
    if cfg.get("audio"):
        desc += " ! audioconvert ! audioresample ! alsasink name=asink sync=false async=false"
    else:
        desc += " ! fakesink name=asink sync=false async=false"
    p = Gst.parse_launch(desc)
    src = p.get_by_name("src")
    src.set_property("ndi-name", cfg["nombre"])
    src.set_property("receiver-ndi-name", cfg.get("equipo") or "NDIICAM")
    if cfg.get("audio"):
        p.get_by_name("asink").set_property("device", cfg["audio"])

    def video(pad, info):
        CONTADORES["rx"] += 1
        if not FORMATO:
            caps = pad.get_current_caps()
            if caps:
                s = caps.get_structure(0)
                ok_n, num, den = s.get_fraction("framerate")
                FORMATO.update(ancho=s.get_value("width"), alto=s.get_value("height"),
                               fps=round(num / den, 2) if ok_n and den else None)
        return Gst.PadProbeReturn.OK

    def audio(pad, info):
        CONTADORES["aud"] += 1
        return Gst.PadProbeReturn.OK

    p.get_by_name("qv").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, video)
    p.get_by_name("asink").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, audio)

    bucle = GLib.MainLoop()
    salida = [0]

    def mensaje(bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            informar({"error": err.message, "elemento": msg.src.get_name()})
            salida[0] = 1
            bucle.quit()
        elif msg.type == Gst.MessageType.EOS:
            # la fuente dejó de emitir
            informar({"error": "eos", "elemento": "src"})
            salida[0] = 1
            bucle.quit()

    bus = p.get_bus()
    bus.add_signal_watch()
    bus.connect("message", mensaje)

    def tic():
        informar({**CONTADORES, **FORMATO, "t": time.monotonic()})
        return True
    GLib.timeout_add(2000, tic)

    def parar(*_):
        bucle.quit()
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, parar)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, parar)

    if p.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        informar({"error": "arranque", "elemento": "pipeline"})
        sys.exit(1)
    bucle.run()
    p.set_state(Gst.State.NULL)
    sys.exit(salida[0])


if __name__ == "__main__":
    main()
