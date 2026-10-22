#!/usr/bin/env python3
# NDIICAM — proceso de una fuente NDI (una cámara, con o sin audio).
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Emite una cámara por NDI y da cuenta al servicio principal.

    fuente.py '<json>'   con dev, ancho, alto, fps, fmt, nombre, audio (o null),
                         hdmi (id del conector DRM del monitor, o null),
                         decodificadores (hilos de decodificación JPEG, 1 o más),
                         gpu (o null): salida H.264 por hardware, ver SalidaGPU, y
                         preview (ruta o null): un JPEG de la cámara cada 2 s, para el panel

Cada fuente va en su propio proceso: si una cámara falla no arrastra a las
demás, y los receptores de cada fuente se distinguen por el proceso que las
atiende. Cada 2 s escribe en la salida una línea JSON con los contadores; si el
pipeline falla, escribe {"error": ..., "elemento": ...} y termina con código 1.
"""
import glob
import json
import os
import re
import shutil
import signal
import sys
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

CONTADORES = {"cam": 0, "bytes": 0, "out": 0, "desc": 0, "aud": 0, "gpu": 0, "gpu_bytes": 0}
NIVEL = {"db_peak": None, "db_rms": None}     # audio, dBFS, lo último que dijo 'level'
Q = "max-size-buffers=3 leaky=downstream"


def informar(datos):
    sys.stdout.write(json.dumps(datos) + "\n")
    sys.stdout.flush()


def liberar_disco(d, reserva):
    """Como un grabador continuo: si el disco se llena, fuera lo más viejo."""
    os.makedirs(d, exist_ok=True)
    viejos = sorted(glob.glob(os.path.join(d, "*.mp4")), key=os.path.getmtime)
    while viejos and shutil.disk_usage(d).free < reserva:
        try:
            os.remove(viejos.pop(0))
        except OSError:
            pass


def descartado(*_):
    CONTADORES["desc"] += 1


def guardar_preview(ruta, cada=2.0):
    """Copia un JPEG de la cámara al disco (tmpfs) cada pocos segundos: la vista
    previa del panel. Sin decodificar nada: es el mismo JPEG que entrega la cámara."""
    ultimo = [0.0]

    def probe(pad, info):
        t = time.monotonic()
        if t - ultimo[0] < cada:
            return Gst.PadProbeReturn.OK
        ultimo[0] = t
        buf = info.get_buffer()
        ok, mapa = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                with open(ruta + ".tmp", "wb") as f:
                    f.write(mapa.data)
                os.replace(ruta + ".tmp", ruta)
            except OSError:
                pass
            finally:
                buf.unmap(mapa)
        return Gst.PadProbeReturn.OK
    return probe


def nivel_audio(msg):
    """Pico y RMS en dBFS (el mayor de los canales) de un mensaje del elemento
    'level', o None si no es uno. GValueArray no siempre pasa a Python: se lee
    del texto del mensaje cuando hace falta."""
    s = msg.get_structure()
    if s is None or s.get_name() != "level":
        return None
    texto = s.to_string()

    def mayor(campo):
        try:
            valores = list(s.get_value(campo))
        except Exception:               # TypeError, ValueError según la versión
            m = re.search(campo + r"=\([^)]*\)[<{]([^>}]*)[>}]", texto)
            valores = [float(x) if x != "-inf" else -120.0 for x in
                       re.findall(r"-inf|-?\d+(?:\.\d+)?(?:e[-+]?\d+)?", m.group(1))] if m else []
        valores = [max(-120.0, min(0.0, float(v))) for v in valores]
        return round(max(valores), 1) if valores else None
    return {"peak": mayor("peak"), "rms": mayor("rms")}


def sonda_retardo(pipeline, acumulado):
    """Sonda para el pad de entrada del sumidero: cuánto lleva cada fotograma en
    la tubería (hora de reloj menos su marca de tiempo de captura). Suma en
    acumulado["suma"]/["n"]; tic() saca la media y pone a cero."""
    def probe(pad, info):
        buf = info.get_buffer()
        reloj = pipeline.get_clock()
        if buf.pts != Gst.CLOCK_TIME_NONE and reloj is not None:
            ahora = reloj.get_time() - pipeline.get_base_time()
            if ahora >= buf.pts:
                acumulado["suma"] += (ahora - buf.pts) / 1e6
                acumulado["n"] += 1
        return Gst.PadProbeReturn.OK
    return probe


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
                "alsasrc name=asrc ! queue name=qa max-size-time=500000000 leaky=downstream"
                " ! audioconvert ! audioresample"
                " ! capsfilter name=af caps=audio/x-raw,format=F32LE,rate=48000"
                # 'level' mide el nivel (dBFS) cada 200 ms para el medidor del panel
                " ! level name=nivel interval=200000000 post-messages=true"
                " ! c.audio ndisinkcombiner name=c ! ndisink name=sink")
    return f"{video} ! ndisink name=sink"


def tramo_camara(cfg):
    if cfg["fmt"] == "MJPG":
        return (f"v4l2src name=src ! image/jpeg,width={cfg['ancho']},height={cfg['alto']},"
                f"framerate={cfg['fps']}/1 ! queue name=q1 {Q}")
    tramo = (f"v4l2src name=src ! video/x-raw,format=YUY2,width={cfg['ancho']},"
             f"height={cfg['alto']},framerate={cfg['fps']}/1 ! queue name=q1 {Q}")
    if cfg.get("preview"):
        # sin JPEG que copiar: la vista previa sale de un fotograma reducido cada
        # 2 s (escalar y comprimir uno de cada 60 cuesta poco)
        tramo += " ! tee name=tp"
    return tramo


def tramo_preview(cfg):
    """Rama de vista previa para el vídeo sin comprimir (va al final de la descripción)."""
    if cfg["fmt"] == "MJPG" or not cfg.get("preview"):
        return ""
    return (" tp. ! queue max-size-buffers=1 leaky=downstream ! videorate drop-only=true"
            " ! video/x-raw,framerate=1/2 ! videoscale ! video/x-raw,width=640,height=360"
            f" ! jpegenc quality=75 ! multifilesink location={cfg['preview']} max-files=1"
            " async=false sync=false")


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


class DecoGPU(Reparto):
    """El JPEG de la cámara lo decodifica la GPU (vajpegdec) y su procesador de
    vídeo lo deja en UYVY (vapostproc): la CPU solo baja la imagen a memoria para
    el NDI. Medido en un i3-8145U a 1080p: entre 1,7 y 4 veces menos CPU que
    jpegdec, más cuanto más detallada es la imagen.

    Mismo esquema que Reparto con un solo decodificador, pero este se crea con el
    primer JPEG: vajpegdec exige el submuestreo en las caps y hay que leerlo de su
    cabecera. Si el JPEG no le vale (progresivo) o la GPU falla, sigue con jpegdec
    sin cortar el NDI y lo avisa."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.n = 1
        self.seq = self.esperado = 0
        self.orden, self.pendientes = {}, {}
        self.caps = None
        self.lock = threading.Lock()
        self.modo = None            # gpu | cpu, al llegar el primer JPEG
        self.deco = None
        self.captura = Gst.parse_launch(
            f"{tramo_camara(cfg)} ! appsink name=cap sync=false emit-signals=true"
            " max-buffers=2 drop=true")
        self.salida = Gst.parse_launch(
            f"appsrc name=ent format=time is-live=true ! {tramo_salida(cfg)}")
        self.entrada = self.salida.get_by_name("ent")
        self.decos = []
        self.captura.get_by_name("cap").connect("new-sample", self._capturado)
        self.pipelines = [self.salida, self.captura]

    def _crear_deco(self, modo, muestreo=None):
        c = self.cfg
        if modo == "gpu":
            caps = (f"image/jpeg,parsed=(boolean)true,width=(int){c['ancho']},height=(int){c['alto']},"
                    f"framerate=(fraction){c['fps']}/1,sof-marker=(int)0,colorspace=(string)sYUV,"
                    f"sampling=(string){muestreo},interlace-mode=(string)progressive")
            desc = (f'appsrc name=a format=time caps="{caps}" ! vajpegdec name=vdec'
                    " ! vapostproc ! video/x-raw,format=UYVY")
        else:
            desc = "appsrc name=a format=time ! jpegdec"
        p = Gst.parse_launch(f"{desc} ! appsink name=s sync=false emit-signals=true")
        p.get_by_name("s").connect("new-sample", self._decodificado)
        p.use_clock(self.captura.get_clock() or Gst.SystemClock.obtain())
        p.set_start_time(Gst.CLOCK_TIME_NONE)
        p.set_base_time(self.captura.get_base_time())
        bus = p.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._error_deco)
        if p.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            p.set_state(Gst.State.NULL)
            return None
        return p

    def _cambiar(self, modo, muestreo=None, motivo=None):
        """Pone el decodificador pedido; si la GPU no arranca, el de CPU."""
        viejo, self.deco = self.deco, None
        if viejo is not None:
            viejo.set_state(Gst.State.NULL)
        p = self._crear_deco(modo, muestreo)
        if p is None and modo == "gpu":
            modo, motivo, p = "cpu", motivo or "arranque", self._crear_deco("cpu")
        if modo == "cpu" and motivo:
            informar({"aviso": "deco_gpu_fallida", "detalle": motivo})
        with self.lock:
            self.orden.clear()
            self.pendientes.clear()
            self.esperado = self.seq
        self.modo, self.deco = modo, p
        self.decos = [p] if p else []

    def _error_deco(self, _bus, msg):
        err, _ = msg.parse_error()
        if self.modo == "gpu":
            self._cambiar("cpu", motivo=err.message)
        else:
            informar({"error": err.message, "elemento": "deco"})

    def _capturado(self, sink):
        buf = sink.emit("pull-sample").get_buffer()
        if self.modo is None:
            ok, info = buf.map(Gst.MapFlags.READ)
            muestreo = muestreo_jpeg(bytes(info.data[:4096])) if ok else None
            if ok:
                buf.unmap(info)
            if muestreo:
                self._cambiar("gpu", muestreo)
            else:
                self._cambiar("cpu", motivo="jpeg_no_admitido")
        with self.lock:
            k = self.seq
            self.seq += 1
            self.orden[buf.pts] = k
        deco = self.deco            # puede cambiar desde el hilo principal si la GPU falla
        if deco is not None:
            deco.get_by_name("a").emit("push-buffer", buf)
        return Gst.FlowReturn.OK

    def parar(self):
        super().parar()
        if self.deco is not None:
            self.deco.set_state(Gst.State.NULL)


class Directo:
    """Un solo pipeline: cámara → (jpegdec) → conversión → NDI."""

    def __init__(self, cfg):
        deco = " ! jpegdec" if cfg["fmt"] == "MJPG" else ""
        self.p = Gst.parse_launch(f"{tramo_camara(cfg)}{deco} ! {tramo_salida(cfg)}{tramo_preview(cfg)}")
        self.pipelines = [self.p]

    def elemento(self, nombre):
        return self.p.get_by_name(nombre)

    def arrancar(self):
        return self.p.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE

    def parar(self):
        self.p.set_state(Gst.State.NULL)


def muestreo_jpeg(datos):
    """Submuestreo de color de un JPEG baseline, leído de su cabecera SOF0.
    vajpegdec lo exige en las caps y jpegparse 1.24 no lo pone. None si el JPEG
    no es baseline (la GPU solo decodifica baseline) o no se entiende."""
    i = 2
    while i + 4 <= len(datos):
        if datos[i] != 0xFF:
            return None
        marca = datos[i + 1]
        if marca == 0xFF:               # relleno entre marcadores
            i += 1
            continue
        largo = int.from_bytes(datos[i + 2:i + 4], "big")
        if marca == 0xC0:
            if i + 12 > len(datos):
                return None
            if datos[i + 9] == 1:
                return "GRAYSCALE"
            h, v = datos[i + 11] >> 4, datos[i + 11] & 15     # factores de la luma
            return {(2, 2): "YCbCr-4:2:0", (2, 1): "YCbCr-4:2:2",
                    (1, 1): "YCbCr-4:4:4"}.get((h, v))
        if 0xC1 <= marca <= 0xCF and marca not in (0xC4, 0xC8, 0xCC) or marca == 0xDA:
            return None                 # progresivo, aritmético... o ya empiezan los datos
        i += 2 + largo
    return None


class SalidaGPU:
    """Salida H.264 por hardware a partir del JPEG de la cámara: la GPU lo
    decodifica y lo codifica sin pasar la imagen por la CPU (bajarla a memoria
    cuesta más que decodificar por CPU, así que el NDI sigue por su camino).

    cfg["gpu"] = {"modo": "grabar" | "srt" | "rtmp", "kbps", "codificador",
                  "dir" (grabar), "puerto" (srt), "url" (rtmp),
                  "cuando": "siempre" | "receptores" | "horario"}

    Con "cuando" distinto de "siempre" arranca en espera: el servicio la enciende y
    apaga con órdenes {"gpu_activa": true|false} por la entrada estándar, según
    haya receptores o sea la hora. Apagarla cierra el fichero en curso.

    Va en su propio pipeline, con el reloj y la hora base del de la cámara para
    que vídeo y audio casen. Recibe copias de los JPEG y del audio: si la GPU, la
    red o el disco fallan, el NDI no se entera; se informa y se reintenta."""

    REINTENTO = 10              # segundos entre intentos tras un fallo
    RESERVA = 2 * 1024 ** 3     # al grabar, borrar lo más viejo si queda menos libre
    SEGMENTO = 10 * 60          # duración de cada fichero de grabación, en segundos

    def __init__(self, cfg, origen, audio, g=None):
        # g: una salida adicional (equipo potente); sin él, la principal, cfg["gpu"]
        self.principal = g is None
        self.g = cfg["gpu"] if g is None else g
        self.cfg = cfg
        self.origen = origen        # pipeline de la cámara: reloj y hora base
        self.audio = audio          # se espera audio
        self.p = None
        self.muestreo = None
        self.caps_audio = None
        self.creando = False
        self.parada = False
        self.habilitada = self.g.get("cuando", "siempre") == "siempre"
        self.espera_hasta = 0.0
        self.primer_jpeg = None
        if not self.habilitada:
            self._estado("en_espera")

    # --- copias desde el pipeline de la cámara (hilos de streaming)
    def jpeg(self, pad, info):
        buf = info.get_buffer()
        p = self.p
        if p is not None:
            p.get_by_name("gj").emit("push-buffer", buf.copy_deep())
        elif (self.habilitada and not self.creando and not self.parada
              and time.monotonic() >= self.espera_hasta):
            ok, mapa = buf.map(Gst.MapFlags.READ)
            if ok:
                self.primer_jpeg = bytes(mapa.data[:4096])
                buf.unmap(mapa)
                self.creando = True
                GLib.idle_add(self._crear)
        return Gst.PadProbeReturn.OK

    def sonido(self, pad, info):
        if self.caps_audio is None:
            caps = pad.get_current_caps()
            if caps:
                self.caps_audio = caps.to_string()
        p = self.p
        if p is not None and self.audio:
            ga = p.get_by_name("ga")
            if ga is not None:
                ga.emit("push-buffer", info.get_buffer().copy_deep())
        return Gst.PadProbeReturn.OK

    # --- construcción (hilo principal)
    def _tramo(self, muestreo):
        g, c = self.g, self.cfg
        caps = (f"image/jpeg,parsed=(boolean)true,width=(int){c['ancho']},height=(int){c['alto']},"
                f"framerate=(fraction){c['fps']}/1,sof-marker=(int)0,colorspace=(string)sYUV,"
                f"sampling=(string){muestreo},interlace-mode=(string)progressive")
        modo = g["modo"]
        # fotograma clave cada 2 s; en SRT cada 1 s, para que quien se conecta a
        # mitad vea imagen antes
        clave = 1 if modo == "srt" else 2
        pad_v = {"grabar": "mux.video", "rtmp": "mux.video", "srt": "mux."}[modo]
        pad_a = {"grabar": "mux.audio_0", "rtmp": "mux.audio", "srt": "mux."}[modo]
        # 4:2:2 y demás: el procesador de vídeo lo pasa a 4:2:0 sin salir de la GPU
        conv = ("" if muestreo == "YCbCr-4:2:0"
                else " ! vapostproc ! video/x-raw(memory:VAMemory),format=NV12")
        v = (f"appsrc name=gj format=time is-live=true caps=\"{caps}\""
             f" ! queue name=qg max-size-buffers=4 leaky=downstream ! vajpegdec{conv}"
             f" ! {g['codificador']} bitrate={int(g['kbps'])} key-int-max={c['fps'] * clave}"
             f" ! h264parse name=hp config-interval=-1 ! queue ! {pad_v}")
        if modo == "grabar":
            # MP4 fragmentado: un corte de luz no deja ilegible el fichero en curso.
            # muxer-properties solo se aplica con async-finalize=true
            mux = (f"splitmuxsink name=mux max-size-time={self.SEGMENTO * Gst.SECOND}"
                   " async-finalize=true muxer-factory=mp4mux"
                   ' muxer-properties="properties,fragment-duration=(uint)1000"')
        elif modo == "srt":
            mux = (f"mpegtsmux name=mux alignment=7 ! srtsink name=red uri=srt://:{int(g['puerto'])}"
                   f" mode=listener latency={int(g.get('latencia', 200))} wait-for-connection=false sync=false")
        else:
            mux = f"flvmux name=mux streamable=true ! rtmp2sink name=red sync=false async-connect=true"
        a = ""
        if self.audio and self.caps_audio:
            a = (f" appsrc name=ga format=time is-live=true caps=\"{self.caps_audio}\""
                 " ! queue max-size-time=1000000000 leaky=downstream ! audioconvert ! audioresample"
                 " ! audio/x-raw,rate=48000,channels=2 ! voaacenc bitrate=128000 ! aacparse"
                 f" ! queue ! {pad_a}")
        return f"{mux} {v}{a}"

    def _crear(self):
        try:
            if self.parada:
                return False
            if self.audio and self.caps_audio is None and self.espera_hasta == 0.0:
                # esperar un poco a que el audio negocie para incluirlo
                self.espera_hasta = time.monotonic() + 0.5
                return False
            self.muestreo = muestreo_jpeg(self.primer_jpeg or b"")
            if not self.muestreo:
                self._fallo("jpeg_no_admitido", "gj", reintentar=False)
                return False
            if self.muestreo != "YCbCr-4:2:0" and not self.g.get("vpp"):
                # el codificador H.264 de la GPU solo acepta 4:2:0 y sin procesador de
                # vídeo (driver iHD libre) no hay quien convierta: no tiene arreglo
                # reintentando. Pasa con capturadoras como la Elgato Cam Link 4K (4:2:2)
                self._fallo("muestreo_no_admitido", "gj", reintentar=False)
                return False
            p = Gst.parse_launch(self._tramo(self.muestreo))
            if self.g["modo"] == "rtmp":
                p.get_by_name("red").set_property("location", self.g["url"])
            if self.g["modo"] == "grabar":
                p.get_by_name("mux").connect("format-location", self._nombre_fichero)
            p.get_by_name("hp").get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER, self._contar)
            bus = p.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self._mensaje)
            p.use_clock(self.origen.get_pipeline_clock())
            p.set_start_time(Gst.CLOCK_TIME_NONE)
            p.set_base_time(self.origen.get_base_time())
            if p.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                p.set_state(Gst.State.NULL)
                self._fallo("arranque", "gpu")
                return False
            self.p = p
            self._estado("activa", muestreo=self.muestreo)
        except GLib.Error as ex:
            self._fallo(ex.message, "gpu")
        finally:
            self.creando = False
        return False

    def _contar(self, pad, info):
        if self.principal:
            CONTADORES["gpu"] += 1
            CONTADORES["gpu_bytes"] += info.get_buffer().get_size()
        return Gst.PadProbeReturn.OK

    def _estado(self, estado, **extra):
        if self.principal:
            informar({"gpu_estado": estado, **extra})
        else:
            informar({"gpu_extra": self.g["modo"], "estado": estado})

    def _nombre_fichero(self, mux, fragmento):
        self._liberar_disco()
        nombre = re.sub(r"[^\w.-]+", "_", self.cfg["nombre"]).strip("_") or "camara"
        return os.path.join(self.g["dir"], f"{nombre}_{time.strftime('%Y-%m-%d_%H-%M-%S')}.mp4")

    def _liberar_disco(self):
        liberar_disco(self.g["dir"], self.RESERVA)

    def _mensaje(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            self._fallo(err.message, msg.src.get_name())

    def _fallo(self, error, elemento, reintentar=True):
        if self.principal:
            informar({"gpu_error": error, "elemento": elemento})
        else:
            informar({"gpu_extra": self.g["modo"], "error": error, "elemento": elemento})
        p, self.p = self.p, None
        if p is not None:
            GLib.idle_add(lambda: p.set_state(Gst.State.NULL) and False)
        self.espera_hasta = time.monotonic() + self.REINTENTO
        self.parada = self.parada or not reintentar

    def habilitar(self, si):
        """Orden del servicio: grabar ahora o parar (cerrando el fichero)."""
        if si == self.habilitada:
            return
        self.habilitada = si
        if si:
            self.espera_hasta = 0.0
            self._estado("iniciando")
        else:
            self.parar(definitivo=False)
            self._estado("en_espera")

    def parar(self, definitivo=True):
        """Cierra limpio (EOS) para que el último fichero quede completo."""
        self.parada = self.parada or definitivo
        p, self.p = self.p, None
        if p is None:
            return
        for nombre in ("gj", "ga"):
            e = p.get_by_name(nombre)
            if e is not None:
                e.emit("end-of-stream")
        p.get_bus().timed_pop_filtered(3 * Gst.SECOND,
                                       Gst.MessageType.EOS | Gst.MessageType.ERROR)
        p.set_state(Gst.State.NULL)


def main():
    cfg = json.loads(sys.argv[1])
    Gst.init(None)
    deco = cfg.get("decodificadores") or 1
    if cfg["fmt"] == "MJPG" and deco == "gpu":
        flujo = DecoGPU(cfg)
    elif cfg["fmt"] == "MJPG" and int(deco) > 1:
        flujo = Reparto(cfg, int(deco))
    else:
        flujo = Directo(cfg)
    flujo.elemento("src").set_property("device", cfg["dev"])
    flujo.elemento("sink").set_property("ndi-name", cfg["nombre"])
    combinador = flujo.elemento("c")
    if cfg.get("baja_latencia") and combinador is not None:
        # la imagen sale sin esperar al audio de su intervalo (plugin parcheado:
        # plugin/ndisinkcombiner-baja-latencia.patch); con el de serie no existe
        if any(p.name == "low-latency" for p in combinador.list_properties()):
            combinador.set_property("low-latency", True)
        else:
            informar({"aviso": "sin_baja_latencia"})
    if cfg.get("audio"):
        flujo.elemento("asrc").set_property("device", cfg["audio"])

    flujo.elemento("src").get_static_pad("src").add_probe(
        Gst.PadProbeType.BUFFER, contar("cam", con_bytes=True))
    flujo.elemento("sink").get_static_pad("sink").add_probe(
        Gst.PadProbeType.BUFFER, contar("out"))
    # retardo interno: de la captura (marca de v4l2src) a la entrada del NDI
    retardo = {"suma": 0.0, "n": 0}
    flujo.elemento("sink").get_static_pad("sink").add_probe(
        Gst.PadProbeType.BUFFER,
        sonda_retardo(flujo.salida if isinstance(flujo, Reparto) else flujo.p, retardo))
    if cfg.get("audio"):
        flujo.elemento("c").get_static_pad("audio").add_probe(
            Gst.PadProbeType.BUFFER, contar("aud"))
    for cola in ("q1", "q2", "q3"):
        flujo.elemento(cola).connect("overrun", descartado)

    if cfg.get("preview"):
        os.makedirs(os.path.dirname(cfg["preview"]), exist_ok=True)
    if cfg.get("preview") and cfg["fmt"] == "MJPG":
        flujo.elemento("src").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, guardar_preview(cfg["preview"]))

    salidas = []
    if cfg.get("gpu") and cfg["fmt"] == "MJPG":
        # el pipeline que tiene la cámara marca el reloj y la hora base
        origen = flujo.captura if isinstance(flujo, Reparto) else flujo.p
        salidas.append(SalidaGPU(cfg, origen, bool(cfg.get("audio"))))
        # en un equipo potente, más salidas a la vez, cada una por su lado
        salidas += [SalidaGPU(cfg, origen, bool(cfg.get("audio")), g)
                    for g in cfg.get("gpu_extra") or []]
        for s in salidas:
            flujo.elemento("src").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, s.jpeg)
            if cfg.get("audio"):
                flujo.elemento("af").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, s.sonido)

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
        elif msg.type == Gst.MessageType.ELEMENT:
            n = nivel_audio(msg)
            if n:
                NIVEL["db_peak"], NIVEL["db_rms"] = n["peak"], n["rms"]

    for p in flujo.pipelines:
        bus = p.get_bus()
        bus.add_signal_watch()
        bus.connect("message", mensaje)

    # tally: lo da el plugin NDI parcheado de NDIICAM (plugin/ndisink-tally.patch);
    # con el plugin de serie no existe la propiedad y no se informa
    sink = flujo.elemento("sink")
    con_tally = any(p.name == "tally-program" for p in sink.list_properties())

    def tic():
        # con su hora: el servicio calcula las tasas entre informes, no entre lecturas
        extra = {}
        if con_tally:
            extra = {"tally_programa": bool(sink.get_property("tally-program")),
                     "tally_previo": bool(sink.get_property("tally-preview")),
                     "conexiones": int(sink.get_property("connections"))}
        if retardo["n"]:
            extra["lat_ms"] = round(retardo["suma"] / retardo["n"], 1)
            retardo["suma"], retardo["n"] = 0.0, 0
        informar({**CONTADORES, **NIVEL, **extra, "t": time.monotonic(),
                  "hilos": ("gpu" if flujo.modo == "gpu" else 1) if isinstance(flujo, DecoGPU)
                  else flujo.n if isinstance(flujo, Reparto) else 1})
        return True
    GLib.timeout_add(2000, tic)

    def parar(*_):
        bucle.quit()

    def orden(canal, _cond):
        # órdenes del servicio, una por línea en JSON
        linea = canal.readline()
        if not linea:
            return False
        try:
            d = json.loads(linea)
        except ValueError:
            return True
        if "gpu_activa" in d:
            # la orden es para la salida de ese tipo (solo grabar tiene condición)
            for s in salidas:
                if d.get("modo") in (None, s.g["modo"]):
                    s.habilitar(bool(d["gpu_activa"]))
        return True
    GLib.io_add_watch(sys.stdin, GLib.IO_IN | GLib.IO_HUP, orden)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, parar)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, parar)

    if not flujo.arrancar():
        informar({"error": "arranque", "elemento": "pipeline"})
        sys.exit(1)
    bucle.run()
    for s in salidas:
        s.parar()
    flujo.parar()
    if cfg.get("preview"):
        for r in (cfg["preview"], cfg["preview"] + ".tmp"):
            try:
                os.remove(r)
            except OSError:
                pass
    sys.exit(salida[0])


if __name__ == "__main__":
    main()
