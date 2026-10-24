#!/usr/bin/env python3
# NDIICAM — relevo: el programa de una ruta, reenviado a YouTube (RTMP) o grabado.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Lee el programa que una ruta publica en MediaMTX y, sin recodificar nada:

    relevo.py  (una línea JSON por la entrada estándar; ver leer_cfg en fuente.py)
                         {"desde": "rtmp://127.0.0.1:1935/live/culto",
                          "modo": "rtmp", "url": "rtmp://a.rtmp.youtube.com/live2/<clave>"}
                         {"desde": ..., "modo": "grabar", "formato": "mp4" | "mkv",
                          "nombre": "Culto" (nombre de la ruta), "ruta": "<id de la ruta>"}
                         (grabar abre una sesión en grabaciones.py: una carpeta con
                         sus trozos; se informa {"sesion": nombre} nada más abrirla)
                         y con_audio: si el programa trae pista de audio (lo que
                         publica NDIICAM la trae siempre; se mira por si acaso)

Un proceso por salida: si YouTube corta o el disco falla, las demás salidas de la
ruta no se enteran. Como los demás procesos: cada 2 s una línea JSON con los
contadores; si falla, {"error": ...} y termina (el servicio reintenta)."""
import json
import os
import signal
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

import grabaciones  # noqa: E402
from fuente import informar, leer_cfg  # noqa: E402

CONTADORES = {"out": 0, "bytes": 0, "aud": 0}
SEGMENTO = 10 * 60          # duración de cada fichero, en segundos


def descripcion(cfg):
    """El pipeline. La rama de audio solo si el programa la trae: un multiplexor
    con una entrada de audio por la que nunca llega nada no escribe ni un byte y
    no da error (se quedaría "esperando" para siempre). La dirección de origen no
    va en el texto: se pone con set_property, como la de destino."""
    cola = "queue max-size-time=3000000000"
    if cfg["modo"] == "rtmp":
        # FLV tal cual: de un RTMP a otro
        desc = (f"rtmp2src name=src ! flvdemux name=d d.video ! {cola} ! h264parse name=hv ! m.video "
                "flvmux name=m streamable=true ! rtmp2sink name=out sync=false async-connect=true")
        if cfg.get("con_audio", True):
            desc += f" d.audio ! {cola} ! aacparse name=ha ! m.audio"
        return desc
    mux = "matroskamux" if cfg.get("formato") == "mkv" else "mp4mux"
    props = "" if mux == "matroskamux" else ' muxer-properties="properties,fragment-duration=(uint)1000"'
    desc = (f"rtmp2src name=src ! flvdemux name=d d.video ! {cola} ! h264parse name=hv ! rec.video "
            f"splitmuxsink name=rec muxer-factory={mux} max-size-time={SEGMENTO * Gst.SECOND}"
            f" async-finalize=true{props}")
    if cfg.get("con_audio", True):
        desc += f" d.audio ! {cola} ! aacparse name=ha ! rec.audio_0"
    return desc


def main():
    cfg = leer_cfg()
    os.nice(5)
    Gst.init(None)
    p = Gst.parse_launch(descripcion(cfg))
    p.get_by_name("src").set_property("location", cfg["desde"])
    if cfg["modo"] == "rtmp":
        p.get_by_name("out").set_property("location", cfg["url"])
    sesion = None
    if cfg["modo"] != "rtmp":
        # una sesión por cada vez que se graba: carpeta con sus trozos y su json
        formato = "mkv" if cfg.get("formato") == "mkv" else "mp4"
        sesion = grabaciones.abrir_sesion(cfg.get("nombre") or cfg.get("prefijo") or "ruta",
                                          cfg.get("ruta"), formato)
        informar({"sesion": sesion})

        def nombre(_mux, _frag):
            # sin tasa conocida: es una copia de lo que llega. Nunca se borra el
            # trozo abierto de esta sesión
            grabaciones.liberar_disco(vivas=(sesion,))
            fichero = grabaciones.nuevo_trozo(sesion)
            if len(grabaciones._leer(sesion)["trozos"]) == 1:
                # la miniatura, cuando el primer trozo lleve unos segundos escritos
                GLib.timeout_add_seconds(5, lambda: (grabaciones.miniatura(sesion), False)[1])
            return fichero
        p.get_by_name("rec").connect("format-location", nombre)

    def contar(clave, con_bytes=False):
        def probe(pad, info):
            CONTADORES[clave] += 1
            if con_bytes:
                CONTADORES["bytes"] += info.get_buffer().get_size()
            return Gst.PadProbeReturn.OK
        return probe
    p.get_by_name("hv").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, contar("out", True))
    ha = p.get_by_name("ha")
    if ha is not None:
        ha.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, contar("aud"))

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
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, bucle.quit)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, bucle.quit)
    if p.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        informar({"error": "arranque", "elemento": "pipeline"})
        sys.exit(1)
    bucle.run()
    # cierre limpio: que el último fichero quede completo
    p.send_event(Gst.Event.new_eos())
    p.get_bus().timed_pop_filtered(3 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    p.set_state(Gst.State.NULL)
    if sesion:
        grabaciones.cerrar_sesion(sesion)
    sys.exit(salida[0])


if __name__ == "__main__":
    main()
