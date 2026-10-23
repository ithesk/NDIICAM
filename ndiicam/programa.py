#!/usr/bin/env python3
# NDIICAM — proceso del programa final (modo puente, edición Pro).
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""El programa que publica el ATEM, con el audio de la consola, listo para repartir.

    programa.py  (una línea JSON por la entrada estándar; ver leer_cfg en fuente.py)
                          con entrada (RTMP local donde publica el ATEM), salida
                          (RTMP local de la ruta 'programa'), audio (dispositivo
                          ALSA de la consola, o null para dejar el del ATEM),
                          con_audio (si lo que llega trae pista de audio),
                          retraso_audio (ms) y grabar ({"dir"} o null)

El vídeo no se recodifica: se copia tal cual. El audio de la consola se captura
aquí, se retrasa (el ATEM tarda en codificar y mandar la imagen, así que la
consola llega antes) y se codifica en AAC. Lo de dentro y lo de fuera tienen
relojes distintos (el del ATEM y el del equipo): el vídeo se recoloca en el
reloj del equipo según cuándo llega, y la deriva entre los dos (en horas, cientos
de ms) se corrige poco a poco. Con grabar, una copia en MKV (vídeo sin tocar) en
trozos de 10 minutos. Como los demás procesos: cada 2 s una línea JSON con los
contadores; si falla, {"error": ...} y termina (el servicio reintenta)."""
import json
import os
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
SEGMENTO = 10 * 60
VENTANA = 10.0          # s: la llegada más temprana en esta ventana marca el desfase
PASO_DERIVA = 0.002     # s por segundo que se corrige la deriva como mucho
ARRANQUE = 10.0         # s tras el primer fotograma en los que el desfase se corrige de golpe


def descripcion(cfg):
    """El pipeline. Las direcciones RTMP no van en el texto: se ponen con
    set_property. Lo que sale lleva siempre pista de audio (la consola, la del
    ATEM o silencio): quien lo lea no tiene que adivinar si vendrá."""
    cola = "queue max-size-time=3000000000 max-size-buffers=0 max-size-bytes=0"
    desc = ("rtmp2src name=src ! flvdemux name=d "
            f"d.video ! {cola} name=qv ! h264parse name=hv config-interval=-1"
            " ! identity name=reloj ! tee name=tv ")
    medir = (" ! volume name=vol ! level name=nivel interval=200000000 post-messages=true"
             " ! audioconvert ! voaacenc bitrate=192000 ! aacparse name=ap ! tee name=ta ")
    if cfg.get("audio"):
        # la consola, retrasada en el pad de 'retraso' (como en fuente.py)
        # el audio del ATEM se descarta, pero enlazado: si no, flvdemux se para (not-linked)
        desc += "d.audio ! queue ! fakesink sync=false async=false "
        desc += (f"alsasrc name=asrc"
                 f" ! queue name=qa max-size-time={(RETRASO_MAX_MS + 500) * 1000000} leaky=downstream"
                 " ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2"
                 " ! volume name=vol ! identity name=retraso"
                 " ! level name=nivel interval=200000000 post-messages=true"
                 " ! audioconvert ! voaacenc bitrate=192000 ! aacparse name=ap ! tee name=ta ")
    elif cfg.get("con_audio", True):
        # el del ATEM: se decodifica y se vuelve a codificar (cuesta poco) para poder
        # ajustarle la ganancia, silenciarlo y medir su nivel
        desc += (f"d.audio ! {cola} name=qa ! aacparse ! avdec_aac ! audioconvert ! audioresample"
                 " ! audio/x-raw,rate=48000,channels=2" + medir)
    else:
        # llega sin audio (un OBS o un móvil con el micro apagado): silencio, para que
        # el programa lleve pista y los relevos y OBS no se queden esperándola. Si al
        # final sí trae, enlazado a nada: que flvdemux no se pare (not-linked)
        desc += ("d.audio ! queue ! fakesink sync=false async=false "
                 "audiotestsrc name=sil wave=silence is-live=true"
                 " ! audio/x-raw,format=S16LE,rate=48000,channels=2 ! queue name=qa" + medir)
    desc += ("flvmux name=m streamable=true latency=500000000"
             " ! rtmp2sink name=out async-connect=true sync=false "
             f"tv. ! {cola} ! m.video ta. ! {cola} ! m.audio ")
    if cfg.get("grabar"):
        desc += (f"splitmuxsink name=rec muxer-factory=matroskamux max-size-time={SEGMENTO * Gst.SECOND}"
                 " async-finalize=true "
                 f"tv. ! {cola} ! rec.video ta. ! {cola} ! rec.audio_0 ")
    return desc


class Reloj:
    """Recoloca el vídeo del ATEM en el reloj del equipo (solo con la consola:
    con el audio del ATEM los dos vienen ya en el mismo reloj)."""

    def __init__(self, p, pad):
        self.p = p
        self.pad = pad              # src de 'reloj': su offset desplaza el vídeo
        self.muestras = []          # (llegada, llegada - pts)
        self.offset = None
        self.inicio = None          # llegada del primer fotograma

    def sonda(self, pad, info):
        buf = info.get_buffer()
        reloj = self.p.get_clock()
        if buf.pts == Gst.CLOCK_TIME_NONE or reloj is None:
            return Gst.PadProbeReturn.OK
        ahora = (reloj.get_time() - self.p.get_base_time()) / 1e9
        seg = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
        pts = buf.pts
        if seg is not None:
            pts = seg.parse_segment().to_running_time(Gst.Format.TIME, buf.pts)
        self.muestras.append((ahora, ahora - pts / 1e9))
        while self.muestras and ahora - self.muestras[0][0] > VENTANA:
            self.muestras.pop(0)
        if self.offset is None:
            # provisional, para que el vídeo tenga ya un tiempo que casa con el del
            # equipo: MediaMTX manda de golpe el último grupo de imágenes al conectar,
            # así que este primer valor va sesgado (hasta un segundo) y corregir()
            # lo lleva enseguida al bueno
            self.inicio = ahora
            self._fijar(ahora - pts / 1e9)
        return Gst.PadProbeReturn.OK

    def _fijar(self, segundos):
        self.offset = segundos
        self.pad.set_offset(int(segundos * 1e9))

    def corregir(self):
        """Cada segundo: el desfase objetivo es la llegada más temprana de la
        ventana (lo que se retrasa la red no cuenta). Los primeros segundos se va
        a él de golpe (la ráfaga inicial sesga el arranque y, si no, tardaría
        minutos); después, despacio, que es la deriva entre los dos relojes."""
        if self.offset is not None and self.muestras:
            objetivo = min(d for _, d in self.muestras)
            dif = objetivo - self.offset
            if abs(dif) > 0.005:
                arrancando = self.muestras[-1][0] - self.inicio < ARRANQUE
                paso = max(-PASO_DERIVA, min(PASO_DERIVA, dif))
                self._fijar(objetivo if arrancando or abs(dif) >= 1 else self.offset + paso)
        return True


def main():
    cfg = leer_cfg()
    os.nice(5)
    Gst.init(None)
    p = Gst.parse_launch(descripcion(cfg))
    p.get_by_name("src").set_property("location", cfg["entrada"])
    p.get_by_name("out").set_property("location", cfg["salida"])
    if cfg.get("audio"):
        p.get_by_name("asrc").set_property("device", cfg["audio"])
    if cfg.get("grabar"):
        d = cfg["grabar"]["dir"]
        os.makedirs(d, exist_ok=True)

        def nombre(_mux, _frag):
            liberar_disco(d)        # sin tasa conocida: es una copia de lo que llega
            return os.path.join(d, f"programa_{time.strftime('%Y-%m-%d_%H-%M-%S')}.mkv")
        p.get_by_name("rec").connect("format-location", nombre)
    vol = p.get_by_name("vol")

    def ganancia(db, mudo):
        vol.set_property("volume", 10 ** (max(-60.0, min(24.0, float(db))) / 20))
        vol.set_property("mute", bool(mudo))
    ganancia(cfg.get("ganancia") or 0, cfg.get("mudo"))
    retraso = p.get_by_name("retraso")
    if retraso is not None:
        retraso.get_static_pad("src").set_offset(
            max(0, min(RETRASO_MAX_MS, int(cfg.get("retraso_audio") or 0))) * 1000000)
    reloj = None
    if cfg.get("audio"):
        r = p.get_by_name("reloj")
        reloj = Reloj(p, r.get_static_pad("src"))
        r.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, reloj.sonda)
        GLib.timeout_add(1000, reloj.corregir)

    def contar(clave, con_bytes=False):
        def probe(pad, info):
            CONTADORES[clave] += 1
            if con_bytes:
                CONTADORES["bytes"] += info.get_buffer().get_size()
            return Gst.PadProbeReturn.OK
        return probe
    p.get_by_name("qv").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, contar("rx"))
    p.get_by_name("ap").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, contar("aud"))
    p.get_by_name("hv").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, contar("out", True))

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
        extra = {"desfase_ms": round(reloj.offset * 1000)} if reloj and reloj.offset is not None else {}
        informar({**CONTADORES, **NIVEL, **extra, "t": time.monotonic()})
        return True
    GLib.timeout_add(2000, tic)

    def orden(canal, _cond):
        linea = canal.readline()
        if not linea:
            return False
        try:
            d = json.loads(linea)
        except ValueError:
            return True
        if "ganancia" in d or "mudo" in d:
            ganancia(d.get("ganancia", 0), d.get("mudo", False))
        if "retraso_audio" in d and retraso is not None:
            # en marcha, sin cortar: se afina de oído
            retraso.get_static_pad("src").set_offset(
                max(0, min(RETRASO_MAX_MS, int(d["retraso_audio"]))) * 1000000)
        return True
    GLib.io_add_watch(sys.stdin, GLib.IO_IN | GLib.IO_HUP, orden)

    def parar(*_):
        bucle.quit()
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, parar)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, parar)

    if p.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        informar({"error": "arranque", "elemento": "pipeline"})
        sys.exit(1)
    bucle.run()
    p.send_event(Gst.Event.new_eos())
    p.get_bus().timed_pop_filtered(3 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    p.set_state(Gst.State.NULL)
    sys.exit(salida[0])


if __name__ == "__main__":
    main()
