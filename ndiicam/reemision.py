#!/usr/bin/env python3
# NDIICAM — proceso de reemisión: una fuente NDI de la red, grabada o emitida con la GPU.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Recibe una fuente NDI de la red (OBS, vMix, otro NDIICAM...) y, sin pasar por
OBS, la graba en el equipo y/o la emite por SRT y RTMP, codificada en la GPU.
Solo en equipos potentes: recibir NDI completo ya cuesta CPU.

    reemision.py '<json>'  con nombre (fuente NDI), equipo (nombre con el que nos
                           ve el emisor), codificador (vah264enc o vah264lpenc),
                           vpp (hay vapostproc), kbps, y salidas: lista de
                           {"modo": "grabar", "dir"} | {"modo": "srt", "puerto",
                           "latencia"} | {"modo": "rtmp", "url"}

Un solo codificador para todas las salidas: cada una recibe una copia del
H.264 y del AAC. El audio va siempre (silencio si la fuente no lo trae): los
multiplexores esperarían por él. Igual que receptor.py: cada 2 s una línea
JSON con los contadores; si el pipeline falla, {"error": ..., "elemento": ...}
y termina (el servicio reintenta)."""
import json
import os
import re
import signal
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from fuente import informar, leer_cfg, liberar_disco, nivel_audio  # noqa: E402
from nucleo import RETRASO_MAX_MS  # noqa: E402

CONTADORES = {"rx": 0, "aud": 0, "out": 0, "bytes": 0}
NIVEL = {"db_peak": None, "db_rms": None}
SEGMENTO = 10 * 60          # duración de cada fichero de grabación, en segundos


def descripcion(cfg):
    """El pipeline: NDI → (GPU) H.264 + AAC → una rama por salida."""
    # NDI entrega UYVY; el codificador quiere NV12. Con procesador de vídeo la
    # conversión la hace la GPU (y la imagen sube a ella de paso)
    if cfg.get("vpp"):
        a_nv12 = "vapostproc ! video/x-raw(memory:VAMemory),format=NV12"
    else:
        # un solo hilo: la conversión va limitada por la memoria y con más hilos
        # solo se añade sincronización (medido en fuente.py: 3 veces más CPU)
        a_nv12 = "videoconvert n-threads=1 ! video/x-raw,format=NV12"
    kbps = int(cfg["kbps"])
    desc = ("ndisrc name=src ! ndisrcdemux name=d "
            "d.video ! queue name=qv max-size-buffers=4 leaky=downstream"
            f" ! {a_nv12} ! {cfg['codificador']} name=enc bitrate={kbps} key-int-max=60"
            " ! tee name=tv "
            # silencio continuo mezclado con el audio (el de la fuente, el de otra
            # entrada o ninguno): los multiplexores necesitan que siempre llegue algo.
            # La ganancia, después de mezclar; el retraso, en el pad de 'retraso'
            "audiotestsrc wave=silence is-live=true"
            " ! audio/x-raw,format=F32LE,rate=48000,channels=2,layout=interleaved"
            " ! audiomixer name=am ! audioconvert ! audioresample ! volume name=vol"
            " ! level name=nivel interval=200000000 post-messages=true"
            " ! audio/x-raw,rate=48000,channels=2 ! voaacenc bitrate=128000"
            " ! tee name=ta ")
    f32 = "audio/x-raw,format=F32LE,rate=48000,channels=2,layout=interleaved"
    if cfg.get("audio_dev"):
        # el de otra entrada (la consola por USB); el de la fuente NDI se descarta
        desc += ("d.audio ! queue ! fakesink sync=false async=false "
                 f"alsasrc name=asrc device={cfg['audio_dev']}"
                 f" ! queue name=qa max-size-time={(RETRASO_MAX_MS + 500) * 1000000} leaky=downstream"
                 f" ! audioconvert ! audioresample ! {f32} ! identity name=retraso ! am. ")
    elif cfg.get("sin_audio"):
        desc += "d.audio ! queue name=qa ! fakesink sync=false async=false "
    else:
        desc += ("d.audio ! queue name=qa max-size-time=300000000 leaky=downstream"
                 f" ! audioconvert ! audioresample ! {f32} ! am. ")
    # cada salida con su parser: el MP4 quiere el H.264 y el AAC en un formato
    # (avc, raw) y MPEG-TS y FLV en otro; un parser común no puede dar los dos
    cola = "queue max-size-time=2000000000 leaky=downstream"
    v = f"{cola} ! h264parse config-interval=-1"
    a = f"{cola} ! aacparse"
    for s in cfg["salidas"]:
        modo = s["modo"]
        if modo == "grabar":
            # MP4 fragmentado: un corte de luz no deja ilegible el fichero en curso
            desc += (f"splitmuxsink name=mux_grabar max-size-time={SEGMENTO * Gst.SECOND}"
                     " async-finalize=true muxer-factory=mp4mux"
                     ' muxer-properties="properties,fragment-duration=(uint)1000" '
                     f"tv. ! {v} ! mux_grabar.video ta. ! {a} ! mux_grabar.audio_0 ")
        elif modo == "srt":
            desc += (f"mpegtsmux name=mux_srt alignment=7 ! srtsink name=red_srt"
                     f" uri=srt://:{int(s['puerto'])} mode=listener"
                     f" latency={int(s.get('latencia', 200))} wait-for-connection=false sync=false "
                     f"tv. ! {v} ! mux_srt. ta. ! {a} ! mux_srt. ")
        elif modo == "rtmp":
            desc += ("flvmux name=mux_rtmp streamable=true"
                     " ! rtmp2sink name=red_rtmp sync=false async-connect=true "
                     f"tv. ! {v} ! mux_rtmp.video ta. ! {a} ! mux_rtmp.audio ")
    return desc


def nombre_fichero(cfg, d):
    def f(_mux, _fragmento):
        liberar_disco(d, kbps=cfg.get("kbps"))
        nombre = re.sub(r"[^\w.-]+", "_", cfg["nombre"]).strip("_") or "ndi"
        return os.path.join(d, f"{nombre}_{time.strftime('%Y-%m-%d_%H-%M-%S')}.mp4")
    return f


def main():
    cfg = leer_cfg()
    os.nice(5)
    Gst.init(None)
    p = Gst.parse_launch(descripcion(cfg))
    src = p.get_by_name("src")
    src.set_property("ndi-name", cfg["nombre"])
    src.set_property("receiver-ndi-name", cfg.get("equipo") or "NDIICAM")
    for s in cfg["salidas"]:
        if s["modo"] == "grabar":
            os.makedirs(s["dir"], exist_ok=True)
            p.get_by_name("mux_grabar").connect("format-location", nombre_fichero(cfg, s["dir"]))
        elif s["modo"] == "rtmp":
            p.get_by_name("red_rtmp").set_property("location", s["url"])

    def contar(clave, con_bytes=False):
        def probe(pad, info):
            CONTADORES[clave] += 1
            if con_bytes:
                CONTADORES["bytes"] += info.get_buffer().get_size()
            return Gst.PadProbeReturn.OK
        return probe
    p.get_by_name("qv").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, contar("rx"))
    p.get_by_name("qa").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, contar("aud"))
    vol, retraso = p.get_by_name("vol"), p.get_by_name("retraso")

    def ajustar(d):
        """Ganancia (dB), silencio y retraso (ms) del audio: al arrancar y en marcha."""
        if "ganancia" in d or "mudo" in d:
            vol.set_property("volume", 10 ** (max(-60.0, min(24.0, float(d.get("ganancia", 0)))) / 20))
            vol.set_property("mute", bool(d.get("mudo", False)))
        if "retraso_audio" in d and retraso is not None:
            retraso.get_static_pad("src").set_offset(
                max(0, min(RETRASO_MAX_MS, int(d["retraso_audio"]))) * 1000000)
    ajustar({"ganancia": cfg.get("ganancia") or 0, "mudo": cfg.get("mudo"),
             "retraso_audio": cfg.get("retraso_audio") or 0})

    def orden(canal, _cond):
        linea = canal.readline()
        if not linea:
            return False
        try:
            ajustar(json.loads(linea))
        except (ValueError, TypeError):
            pass
        return True
    GLib.io_add_watch(sys.stdin, GLib.IO_IN | GLib.IO_HUP, orden)
    p.get_by_name("enc").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, contar("out", True))

    bucle = GLib.MainLoop()
    salida = [0]

    def mensaje(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            informar({"error": err.message, "elemento": msg.src.get_name()})
            salida[0] = 1
            bucle.quit()
        elif msg.type == Gst.MessageType.EOS:
            informar({"error": "eos", "elemento": "src"})
            salida[0] = 1
            bucle.quit()
        elif msg.type == Gst.MessageType.ELEMENT:
            n = nivel_audio(msg)
            if n:
                NIVEL["db_peak"], NIVEL["db_rms"] = n["peak"], n["rms"]

    bus = p.get_bus()
    bus.add_signal_watch()
    bus.connect("message", mensaje)

    def tic():
        informar({**CONTADORES, **NIVEL, "t": time.monotonic()})
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
    # cierre limpio: que el último fichero quede completo
    p.send_event(Gst.Event.new_eos())
    p.get_bus().timed_pop_filtered(3 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    p.set_state(Gst.State.NULL)
    sys.exit(salida[0])


if __name__ == "__main__":
    main()
