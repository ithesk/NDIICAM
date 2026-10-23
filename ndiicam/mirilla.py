#!/usr/bin/env python3
# NDIICAM — mirilla: la vista previa de una cámara que ninguna ruta usa.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Con el motor de rutas, una cámara solo tiene proceso cuando una ruta la usa, y
sin proceso no hay vista previa en el panel: para ver qué entra había que encender
una ruta. La mirilla abre la cámara en su modo más pequeño y guarda un JPEG cada
dos segundos, nada más. El servicio la arranca mientras alguien mira el panel y la
quita en cuanto una ruta necesita la cámara (o nadie mira).

    mirilla.py '<json>'   con dev, fmt (MJPG | YUYV), ancho, alto, fps y preview
                          (ruta del JPEG). Como los demás: una línea JSON cada 2 s."""
import json
import signal
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from fuente import guardar_preview, informar, leer_cfg  # noqa: E402

CONTADORES = {"out": 0, "bytes": 0, "aud": 0}
CADA = 2.0      # segundos entre vistas previas


def descripcion(cfg):
    src = f"v4l2src device={cfg['dev']} ! "
    if cfg["fmt"] == "MJPG":
        # el JPEG de la cámara tal cual: ni decodificar ni codificar
        return src + (f"image/jpeg,width={cfg['ancho']},height={cfg['alto']},framerate={cfg['fps']}/1"
                      " ! identity name=i ! fakesink sync=false async=false")
    # sin comprimir: solo se codifica una imagen cada dos segundos
    return src + (f"video/x-raw,format=YUY2,width={cfg['ancho']},height={cfg['alto']},framerate={cfg['fps']}/1"
                  " ! videorate drop-only=true ! video/x-raw,framerate=1/2 ! videoconvert"
                  " ! jpegenc quality=70 ! identity name=i ! fakesink sync=false async=false")


def main():
    cfg = leer_cfg()
    Gst.init(None)
    p = Gst.parse_launch(descripcion(cfg))
    pad = p.get_by_name("i").get_static_pad("src")
    guardar = guardar_preview(cfg["preview"], CADA if cfg["fmt"] == "MJPG" else 0.0)

    def sonda(pad, info):
        CONTADORES["out"] += 1
        return guardar(pad, info)
    pad.add_probe(Gst.PadProbeType.BUFFER, sonda)

    bucle = GLib.MainLoop()
    salida = [0]

    def mensaje(_bus, msg):
        if msg.type in (Gst.MessageType.ERROR, Gst.MessageType.EOS):
            if msg.type == Gst.MessageType.ERROR:
                err, _ = msg.parse_error()
                informar({"error": err.message, "elemento": msg.src.get_name()})
            else:
                informar({"error": "eos", "elemento": "src"})
            salida[0] = 1
            bucle.quit()
    bus = p.get_bus()
    bus.add_signal_watch()
    bus.connect("message", mensaje)
    GLib.timeout_add(2000, lambda: (informar({**CONTADORES, "t": time.monotonic()}), True)[1])
    for s in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, s, lambda *_: bucle.quit())
    if p.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        informar({"error": "arranque", "elemento": "pipeline"})
        sys.exit(1)
    bucle.run()
    p.set_state(Gst.State.NULL)
    sys.exit(salida[0])


if __name__ == "__main__":
    main()
