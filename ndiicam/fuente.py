#!/usr/bin/env python3
# NDIICAM — proceso de una fuente NDI (una cámara, con o sin audio).
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Emite una cámara por NDI y da cuenta al servicio principal.

    fuente.py '<json>'   con dev, ancho, alto, fps, fmt, nombre, audio (o null) y
                         hdmi (id del conector DRM del monitor, o null)

Cada fuente va en su propio proceso: si una cámara falla no arrastra a las
demás, y los receptores de cada fuente se distinguen por el proceso que las
atiende. Cada 2 s escribe en la salida una línea JSON con los contadores; si el
pipeline falla, escribe {"error": ..., "elemento": ...} y termina con código 1.
"""
import json
import os
import signal
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

CONTADORES = {"cam": 0, "bytes": 0, "out": 0, "desc": 0, "aud": 0}


def informar(datos):
    sys.stdout.write(json.dumps(datos) + "\n")
    sys.stdout.flush()


def main():
    cfg = json.loads(sys.argv[1])
    Gst.init(None)
    q = "max-size-buffers=3 leaky=downstream"
    if cfg["fmt"] == "MJPG":
        video = (f"v4l2src name=src ! image/jpeg,width={cfg['ancho']},height={cfg['alto']},"
                 f"framerate={cfg['fps']}/1 ! queue name=q1 {q} ! jpegdec")
    else:
        video = (f"v4l2src name=src ! video/x-raw,format=YUY2,width={cfg['ancho']},"
                 f"height={cfg['alto']},framerate={cfg['fps']}/1 ! queue name=q1 {q}")
    # colas entre etapas: decode, conversión y envío en hilos distintos (en un
    # solo hilo un Atom no pasa de ~22 fps a 1080p)
    video += (f" ! queue name=q2 {q} ! videoconvert n-threads={os.cpu_count() or 2}"
              f" ! video/x-raw,format=UYVY ! queue name=q3 {q}")
    if cfg.get("hdmi"):
        # copia al HDMI como monitor de encuadre: sin sincronizar y con una cola
        # que descarta, para no frenar nunca el NDI. UYVY va a un plano de vídeo
        # de la GPU sin pasar por la CPU. Esos planos no siempre escalan (Cherry
        # Trail no), así que para verla a pantalla completa la pantalla tiene que
        # arrancar a la resolución de la cámara (ajuste "hdmi_consola")
        video += (" ! tee name=t t. ! queue name=qh max-size-buffers=1 leaky=downstream"
                  f" ! kmssink name=hdmi sync=false connector-id={cfg['hdmi']}"
                  " t. ! queue name=q4 max-size-buffers=2 leaky=downstream")
    if cfg.get("audio"):
        desc = (f"{video} ! c.video "
                f"alsasrc name=asrc ! queue name=qa max-size-time=500000000 leaky=downstream"
                " ! audioconvert ! audioresample ! audio/x-raw,format=F32LE,rate=48000"
                " ! c.audio ndisinkcombiner name=c ! ndisink name=sink")
    else:
        desc = f"{video} ! ndisink name=sink"
    p = Gst.parse_launch(desc)
    p.get_by_name("src").set_property("device", cfg["dev"])
    p.get_by_name("sink").set_property("ndi-name", cfg["nombre"])
    if cfg.get("audio"):
        p.get_by_name("asrc").set_property("device", cfg["audio"])

    def contar(clave, con_bytes=False):
        def probe(pad, info):
            CONTADORES[clave] += 1
            if con_bytes:
                CONTADORES["bytes"] += info.get_buffer().get_size()
            return Gst.PadProbeReturn.OK
        return probe

    p.get_by_name("src").get_static_pad("src").add_probe(
        Gst.PadProbeType.BUFFER, contar("cam", con_bytes=True))
    p.get_by_name("sink").get_static_pad("sink").add_probe(
        Gst.PadProbeType.BUFFER, contar("out"))
    if cfg.get("audio"):
        p.get_by_name("c").get_static_pad("audio").add_probe(
            Gst.PadProbeType.BUFFER, contar("aud"))
    for cola in ("q1", "q2", "q3"):
        p.get_by_name(cola).connect("overrun", lambda *_: CONTADORES.__setitem__(
            "desc", CONTADORES["desc"] + 1))

    bucle = GLib.MainLoop()
    salida = [0]

    def mensaje(bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            informar({"error": err.message, "elemento": msg.src.get_name()})
            salida[0] = 1
            bucle.quit()
        elif msg.type == Gst.MessageType.EOS:
            informar({"error": "eos", "elemento": "src"})
            salida[0] = 1
            bucle.quit()

    bus = p.get_bus()
    bus.add_signal_watch()
    bus.connect("message", mensaje)

    def tic():
        # con su hora: el servicio calcula las tasas entre informes, no entre lecturas
        informar({**CONTADORES, "t": time.monotonic()})
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
