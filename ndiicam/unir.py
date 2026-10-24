#!/usr/bin/env python3
# NDIICAM — unir los trozos de una sesión de grabación y sacar su miniatura.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Trabajos sueltos sobre una grabación, que grabaciones.py lanza en segundo plano:

    unir.py unir <carpeta> <mp4|mkv>     trozo-*.<ext> → completa.<ext> (copia sin
                                         recodificar; se escribe en .tmp y se renombra)
    unir.py miniatura <fichero> <jpg>    el primer fotograma, a 640 px de ancho

Sale con 0 si acabó bien y con 1 si no; lo que escribía a medias se borra."""
import glob
import os
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

TIEMPO_MAX = 20 * 60    # s para unir (un culto largo a velocidad de disco)
TIEMPO_MINI = 20        # s para la miniatura


def _correr(p, segundos):
    """Hasta EOS o error, con tope. Devuelve True si acabó bien."""
    bucle = GLib.MainLoop()
    bien = [False]

    def mensaje(_bus, msg):
        if msg.type == Gst.MessageType.EOS:
            bien[0] = True
            bucle.quit()
        elif msg.type == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            sys.stderr.write(err.message + "\n")
            bucle.quit()
    bus = p.get_bus()
    bus.add_signal_watch()
    bus.connect("message", mensaje)
    GLib.timeout_add_seconds(segundos, bucle.quit)
    if p.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        return False
    bucle.run()
    p.set_state(Gst.State.NULL)
    return bien[0]


def unir(carpeta, ext):
    trozos = sorted(glob.glob(os.path.join(carpeta, f"trozo-*.{ext}")))
    if not trozos:
        return False
    salida = os.path.join(carpeta, f"completa.{ext}")
    tmp = salida + ".tmp"
    mux = "matroskamux" if ext == "mkv" else "mp4mux faststart=true"
    # dos cadenas sueltas: splitmuxsrc crea sus pads al arrancar y aquí se enlazan
    # al multiplexor según aparecen (vídeo y audio)
    p = Gst.parse_launch(f"splitmuxsrc name=src  {mux} name=mux ! filesink name=out")
    p.get_by_name("src").set_property("location", os.path.join(carpeta, f"trozo-*.{ext}"))
    p.get_by_name("out").set_property("location", tmp)
    mux_el = p.get_by_name("mux")

    def pad_nuevo(src, pad):
        nombre = pad.get_name()
        plantilla = "video_%u" if nombre.startswith("video") else "audio_%u"
        destino = mux_el.request_pad_simple(plantilla) if hasattr(mux_el, "request_pad_simple") \
            else mux_el.get_request_pad(plantilla)
        if destino is None or pad.link(destino) != Gst.PadLinkReturn.OK:
            sys.stderr.write(f"no se enlaza {nombre}\n")
    p.get_by_name("src").connect("pad-added", pad_nuevo)
    bien = _correr(p, TIEMPO_MAX)
    if bien and os.path.exists(tmp):
        os.replace(tmp, salida)
        return True
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


def miniatura(fichero, jpg):
    demux = "matroskademux" if fichero.endswith(".mkv") else "qtdemux"
    p = Gst.parse_launch(
        f"filesrc name=src ! {demux} name=d d.video_0 ! queue ! h264parse ! avdec_h264 ! videoconvert"
        " ! videoscale ! video/x-raw,width=640 ! jpegenc ! filesink name=out")
    p.get_by_name("src").set_property("location", fichero)
    tmp = jpg + ".tmp"
    p.get_by_name("out").set_property("location", tmp)
    # un solo fotograma: tras el primero que llega al filesink, EOS
    out = p.get_by_name("out")
    visto = [False]

    def sonda(pad, info):
        if visto[0]:
            return Gst.PadProbeReturn.DROP
        visto[0] = True
        GLib.idle_add(lambda: (p.send_event(Gst.Event.new_eos()), False)[1])
        return Gst.PadProbeReturn.OK
    out.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, sonda)
    bien = _correr(p, TIEMPO_MINI)
    if bien and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, jpg)
        return True
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    Gst.init(None)
    if sys.argv[1] == "unir":
        sys.exit(0 if unir(sys.argv[2], sys.argv[3]) else 1)
    if sys.argv[1] == "miniatura":
        sys.exit(0 if miniatura(sys.argv[2], sys.argv[3]) else 1)
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
