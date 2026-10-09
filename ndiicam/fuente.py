#!/usr/bin/env python3
# NDIICAM — proceso de una fuente NDI (una cámara, con o sin audio).
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Emite una cámara por NDI y da cuenta al servicio principal.

    fuente.py '<json>'   con dev, ancho, alto, fps, fmt, nombre, audio (o null),
                         hdmi (id del conector DRM del monitor, o null) y
                         decodificadores (hilos de decodificación JPEG, 1 o más)

Cada fuente va en su propio proceso: si una cámara falla no arrastra a las
demás, y los receptores de cada fuente se distinguen por el proceso que las
atiende. Cada 2 s escribe en la salida una línea JSON con los contadores; si el
pipeline falla, escribe {"error": ..., "elemento": ...} y termina con código 1.
"""
import json
import os
import signal
import sys
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

CONTADORES = {"cam": 0, "bytes": 0, "out": 0, "desc": 0, "aud": 0}
Q = "max-size-buffers=3 leaky=downstream"


def informar(datos):
    sys.stdout.write(json.dumps(datos) + "\n")
    sys.stdout.flush()


def descartado(*_):
    CONTADORES["desc"] += 1


def contar(clave, con_bytes=False):
    def probe(pad, info):
        CONTADORES[clave] += 1
        if con_bytes:
            CONTADORES["bytes"] += info.get_buffer().get_size()
        return Gst.PadProbeReturn.OK
    return probe


def tramo_salida(cfg):
    """De los fotogramas decodificados al NDI (y al HDMI si toca)."""
    # colas entre etapas: conversión y envío en hilos distintos (en un solo
    # hilo un Atom no pasa de ~22 fps a 1080p)
    video = (f"queue name=q2 {Q} ! videoconvert n-threads={os.cpu_count() or 2}"
             f" ! video/x-raw,format=UYVY ! queue name=q3 {Q}")
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
        return (f"{video} ! c.video "
                f"alsasrc name=asrc ! queue name=qa max-size-time=500000000 leaky=downstream"
                " ! audioconvert ! audioresample ! audio/x-raw,format=F32LE,rate=48000"
                " ! c.audio ndisinkcombiner name=c ! ndisink name=sink")
    return f"{video} ! ndisink name=sink"


def tramo_camara(cfg):
    if cfg["fmt"] == "MJPG":
        return (f"v4l2src name=src ! image/jpeg,width={cfg['ancho']},height={cfg['alto']},"
                f"framerate={cfg['fps']}/1 ! queue name=q1 {Q}")
    return (f"v4l2src name=src ! video/x-raw,format=YUY2,width={cfg['ancho']},"
            f"height={cfg['alto']},framerate={cfg['fps']}/1 ! queue name=q1 {Q}")


class Reparto:
    """Decodificación JPEG en varios hilos. jpegdec va en uno solo: con imágenes
    muy detalladas (noche con grano, público, follaje) un núcleo no llega a
    30 fps a 1080p aunque los demás estén parados. Aquí los fotogramas se
    reparten por turnos entre N decodificadores y se devuelven en orden.

    Tres pipelines: cámara → appsink, N × (appsrc → jpegdec → appsink) y
    appsrc → conversión → NDI. Todos con el mismo reloj y la misma hora de
    partida, para que los sellos de tiempo de vídeo y audio sigan casando."""

    def __init__(self, cfg, n):
        self.n = n
        self.seq = 0                # orden de llegada del próximo JPEG
        self.esperado = 0           # orden del próximo que debe salir
        self.orden = {}             # pts -> orden (jpegdec no conserva el offset)
        self.pendientes = {}        # orden -> fotograma decodificado fuera de turno
        self.caps = None
        self.lock = threading.Lock()
        self.captura = Gst.parse_launch(
            f"{tramo_camara(cfg)} ! appsink name=cap sync=false emit-signals=true"
            " max-buffers=2 drop=true")
        self.salida = Gst.parse_launch(
            f"appsrc name=ent format=time is-live=true ! {tramo_salida(cfg)}")
        self.entrada = self.salida.get_by_name("ent")
        self.decos = []
        for _ in range(n):
            p = Gst.parse_launch("appsrc name=a format=time ! jpegdec"
                                 " ! appsink name=s sync=false emit-signals=true")
            p.get_by_name("s").connect("new-sample", self._decodificado)
            self.decos.append(p)
        self.captura.get_by_name("cap").connect("new-sample", self._capturado)
        self.pipelines = [self.salida, *self.decos, self.captura]

    def _capturado(self, sink):
        buf = sink.emit("pull-sample").get_buffer()
        with self.lock:
            k = self.seq
            self.seq += 1
            self.orden[buf.pts] = k
        self.decos[k % self.n].get_by_name("a").emit("push-buffer", buf)
        return Gst.FlowReturn.OK

    def _decodificado(self, sink):
        m = sink.emit("pull-sample")
        buf, caps = m.get_buffer(), m.get_caps()
        listos = []
        with self.lock:
            k = self.orden.pop(buf.pts, None)
            if k is None:
                listos.append(buf)      # sin número: fuera como venga
            else:
                self.pendientes[k] = buf
            while self.pendientes:
                if self.esperado in self.pendientes:
                    listos.append(self.pendientes.pop(self.esperado))
                    self.esperado += 1
                elif len(self.pendientes) >= self.n:
                    # falta uno y los demás ya están: se perdió, no esperarlo
                    self.esperado = min(self.pendientes)
                else:
                    break
            if listos and (self.caps is None or not caps.is_equal(self.caps)):
                self.caps = caps
                self.entrada.set_property("caps", caps)
        for b in listos:
            self.entrada.emit("push-buffer", b)
        return Gst.FlowReturn.OK

    def elemento(self, nombre):
        for p in self.pipelines:
            e = p.get_by_name(nombre)
            if e:
                return e
        return None

    def arrancar(self):
        reloj = Gst.SystemClock.obtain()
        base = reloj.get_time()
        for p in self.pipelines:
            p.use_clock(reloj)
            p.set_start_time(Gst.CLOCK_TIME_NONE)
            p.set_base_time(base)
        return all(p.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE
                   for p in self.pipelines)

    def parar(self):
        for p in self.pipelines:
            p.set_state(Gst.State.NULL)


class Directo:
    """Un solo pipeline: cámara → (jpegdec) → conversión → NDI."""

    def __init__(self, cfg):
        deco = " ! jpegdec" if cfg["fmt"] == "MJPG" else ""
        self.p = Gst.parse_launch(f"{tramo_camara(cfg)}{deco} ! {tramo_salida(cfg)}")
        self.pipelines = [self.p]

    def elemento(self, nombre):
        return self.p.get_by_name(nombre)

    def arrancar(self):
        return self.p.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE

    def parar(self):
        self.p.set_state(Gst.State.NULL)


def main():
    cfg = json.loads(sys.argv[1])
    Gst.init(None)
    n = int(cfg.get("decodificadores") or 1)
    flujo = Reparto(cfg, n) if cfg["fmt"] == "MJPG" and n > 1 else Directo(cfg)
    flujo.elemento("src").set_property("device", cfg["dev"])
    flujo.elemento("sink").set_property("ndi-name", cfg["nombre"])
    if cfg.get("audio"):
        flujo.elemento("asrc").set_property("device", cfg["audio"])

    flujo.elemento("src").get_static_pad("src").add_probe(
        Gst.PadProbeType.BUFFER, contar("cam", con_bytes=True))
    flujo.elemento("sink").get_static_pad("sink").add_probe(
        Gst.PadProbeType.BUFFER, contar("out"))
    if cfg.get("audio"):
        flujo.elemento("c").get_static_pad("audio").add_probe(
            Gst.PadProbeType.BUFFER, contar("aud"))
    for cola in ("q1", "q2", "q3"):
        flujo.elemento(cola).connect("overrun", descartado)

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

    for p in flujo.pipelines:
        bus = p.get_bus()
        bus.add_signal_watch()
        bus.connect("message", mensaje)

    def tic():
        # con su hora: el servicio calcula las tasas entre informes, no entre lecturas
        informar({**CONTADORES, "t": time.monotonic(), "hilos": n if isinstance(flujo, Reparto) else 1})
        return True
    GLib.timeout_add(2000, tic)

    def parar(*_):
        bucle.quit()
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, parar)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, parar)

    if not flujo.arrancar():
        informar({"error": "arranque", "elemento": "pipeline"})
        sys.exit(1)
    bucle.run()
    flujo.parar()
    sys.exit(salida[0])


if __name__ == "__main__":
    main()
