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

from nucleo import RETRASO_MAX_MS  # noqa: E402

CONTADORES = {"cam": 0, "bytes": 0, "out": 0, "desc": 0, "aud": 0, "gpu": 0, "gpu_bytes": 0}
NIVEL = {"db_peak": None, "db_rms": None}     # audio, dBFS, lo último que dijo 'level'
Q = "max-size-buffers=3 leaky=downstream"


def informar(datos):
    sys.stdout.write(json.dumps(datos) + "\n")
    sys.stdout.flush()


def alquiler(cfg):
    """kmssink sobre la pantalla que alquila el servicio (ver pantallas.py), con
    skip-vsync: el cambio de página no se deja en un alquiler, y el plano ya
    espera al refresco por su cuenta. Sin alquiler, kmssink abre la tarjeta."""
    # restore-crtc=false: al soltar la pantalla no se vuelve a lo que había antes
    # (la consola de Linux); se queda la última imagen hasta que otro pinte
    return f" fd={cfg['drm_fd']} skip-vsync=true restore-crtc=false" if cfg.get("drm_fd") else ""


RESERVA_MINIMA = 2 * 1024 ** 3      # lo que tiene que quedar libre, como poco
RESERVA_SIN_TASA = 4 * 1024 ** 3    # si no se sabe a cuánto se graba (una copia del ATEM)
SEGMENTO_S = 10 * 60                # duración de cada fichero de grabación


def leer_cfg():
    """La configuración del proceso: una línea JSON por la entrada estándar (así las
    claves de emisión no quedan a la vista en la lista de procesos) o, a mano desde
    una consola, como primer argumento."""
    if len(sys.argv) > 1:
        return json.loads(sys.argv[1])
    return json.loads(sys.stdin.readline())


def liberar_disco(d, reserva=None, kbps=None):
    """Como un grabador continuo: si el disco se llena, fuera lo más viejo. Lo hace
    grabaciones.py (sesiones y ficheros sueltos, respetando lo protegido y el trozo
    que se está escribiendo); aquí queda la firma de siempre para los procesos. Si
    d no es la carpeta de grabaciones (una prueba), se limpia esa carpeta a lo viejo."""
    import grabaciones
    if os.path.realpath(d) == os.path.realpath(grabaciones.DIR):
        return grabaciones.liberar_disco(reserva, kbps)
    reserva = grabaciones.reserva_disco(reserva, kbps)
    os.makedirs(d, exist_ok=True)
    viejos = sorted(glob.glob(os.path.join(d, "*.mp4")) + glob.glob(os.path.join(d, "*.mkv")),
                    key=os.path.getmtime)
    borrados = []
    while viejos and shutil.disk_usage(d).free < reserva:
        try:
            os.remove(viejos[0])
            borrados.append(viejos.pop(0))
        except OSError:
            viejos.pop(0)
    return borrados


def fijar_retraso(flujo, ms):
    """Retrasa el audio ms milisegundos respecto a la imagen; se puede cambiar en marcha."""
    ns = max(0, min(RETRASO_MAX_MS, int(ms))) * 1000000
    retraso = flujo.elemento("retraso")
    if retraso is None:
        return 0
    retraso.get_static_pad("src").set_offset(ns)
    qr = flujo.elemento("qr")
    if qr is not None:
        qr.set_property("min-threshold-time", ns)
    return ns


def analizar_calibracion(audio, video):
    """Desfase entre audio e imagen a partir de unas palmadas delante de la cámara.

    audio: [(t, pico)] cada milisegundo; video: [(t, cambio)] por fotograma (cuánto
    cambió la imagen respecto a la anterior). Tiempos en segundos, en el reloj del
    pipeline y sin retraso aplicado. Cada palmada es un golpe seco en el audio; en
    la imagen, el fotograma de más movimiento cerca de él (las manos chocan y se
    paran). Devuelve {"ms": mediana de imagen menos audio, "palmadas": n,
    "dispersion": ms entre la mayor y la menor} o {"error": ...}."""
    if not audio or len(video) < 3:
        return {"error": "sin_datos"}
    picos = sorted(v for _, v in audio)
    fondo = picos[len(picos) // 2]
    umbral = max(0.08, 6 * fondo, 0.3 * picos[-1])
    golpes, ultimo = [], None
    for t, v in audio:
        # un golpe nuevo si supera el umbral tras un rato de calma (las palmadas
        # tienen eco y rebote: lo que viene en los 300 ms siguientes es la misma)
        if v >= umbral and (ultimo is None or t - ultimo > 0.3):
            golpes.append(t)
        if v >= umbral / 2:
            ultimo = t
    if not golpes:
        return {"error": "sin_palmadas"}
    medidas = []
    for i, ta in enumerate(golpes):
        # la imagen suele venir detrás (hasta RETRASO_MAX_MS), pero también se mira
        # medio segundo antes, para avisar si es el audio el que va tarde; sin
        # pisar la palmada anterior ni la siguiente
        ini = max(ta - 0.5, (golpes[i - 1] + ta) / 2 if i else -1e9)
        fin = min(ta + RETRASO_MAX_MS / 1000, golpes[i + 1] - 0.05 if i + 1 < len(golpes) else 1e9)
        ventana = [(c, t) for t, c in video if ini <= t <= fin]
        if len(ventana) < 2:
            continue
        cambio, tv = max(ventana)
        resto = sorted(c for c, _ in ventana)
        if cambio > 3 * resto[len(resto) // 2] + 1:     # se movió algo de verdad
            medidas.append((tv - ta) * 1000)
    if not medidas:
        return {"error": "sin_movimiento", "palmadas": len(golpes)}
    medidas.sort()
    return {"ms": round(medidas[len(medidas) // 2]), "palmadas": len(medidas),
            "dispersion": round(medidas[-1] - medidas[0])}


CALIBRANDO = {}


def calibrar(flujo, segundos):
    """Escucha y mira unos segundos y luego informa {"calibracion": {...}}."""
    if CALIBRANDO or flujo.elemento("retraso") is None:
        informar({"calibracion": {"error": "sin_audio" if not CALIBRANDO else "ocupado"}})
        return
    audio, video, previo = [], [], [None]
    CALIBRANDO["activo"] = True

    def oir(pad, info):
        buf = info.get_buffer()
        caps = pad.get_current_caps()
        if buf.pts == Gst.CLOCK_TIME_NONE or caps is None:
            return Gst.PadProbeReturn.OK
        st = caps.get_structure(0)
        ok_c, canales = st.get_int("channels")
        canales = canales if ok_c and canales else 1
        ok, mapa = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        try:
            muestras = memoryview(mapa.data).cast("f")
            bloque = 48 * canales                   # 1 ms a 48 kHz
            t0 = buf.pts / 1e9
            for i in range(0, len(muestras) - bloque + 1, bloque):
                trozo = muestras[i:i + bloque]
                audio.append((t0 + i / canales / 48000, max(max(trozo), -min(trozo))))
        finally:
            buf.unmap(mapa)
        return Gst.PadProbeReturn.OK

    def mirar(pad, info):
        buf = info.get_buffer()
        if buf.pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        ok, mapa = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        try:
            datos = mapa.data
            # unas 8000 muestras repartidas por la imagen bastan para ver el movimiento.
            # Salto impar: en UYVY/YUY2 uno par caería siempre en el color o siempre en
            # la luz, y un destello blanco o una mano en gris no cambian el color
            muestra = bytes(memoryview(datos)[::max(1, len(datos) // 8000) | 1])
        finally:
            buf.unmap(mapa)
        if previo[0] is not None and len(previo[0]) == len(muestra):
            cambio = sum(abs(x - y) for x, y in zip(muestra, previo[0])) / len(muestra)
            video.append((buf.pts / 1e9, cambio))
        previo[0] = muestra
        return Gst.PadProbeReturn.OK

    pa = flujo.elemento("retraso").get_static_pad("sink")
    pv = flujo.elemento("q2").get_static_pad("sink")
    sondas = [(pa, pa.add_probe(Gst.PadProbeType.BUFFER, oir)),
              (pv, pv.add_probe(Gst.PadProbeType.BUFFER, mirar))]

    def terminar():
        for pad, sonda in sondas:
            pad.remove_probe(sonda)
        CALIBRANDO.clear()
        informar({"calibracion": analizar_calibracion(audio, video)})
        return False
    GLib.timeout_add(int(segundos * 1000), terminar)


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


def con_ndi(cfg):
    """Si esta cámara sale por NDI. Sin NDI (solo monitor, o una ruta que solo publica
    o graba) la imagen va al HDMI o a ninguna parte, y el audio sigue para la GPU."""
    return cfg.get("ndi", True) and not cfg.get("solo_monitor")


def tramo_salida(cfg):
    """De los fotogramas decodificados al NDI (y al HDMI si toca). El sumidero final
    se llama siempre 'sink' (el NDI, o el HDMI/fakesink sin NDI) para que contadores
    y retardo funcionen igual."""
    ndi = con_ndi(cfg)
    # colas entre etapas: conversión y envío en hilos distintos (en un solo
    # hilo un Atom no pasa de ~22 fps a 1080p). La conversión en varios hilos solo
    # compensa al pasar del I420 de jpegdec a UYVY; reordenar YUY2 a UYVY (o no
    # tocar el UYVY que ya da la GPU) va limitado por la memoria, y los hilos solo
    # añaden sincronización: medido, 3,2 ms por fotograma con 4 frente a 1,0 con 1
    hilos = 1 if cfg.get("fmt") not in ("MJPG", "H264") or cfg.get("decodificadores") == "gpu" \
        else (os.cpu_count() or 2)
    video = (f"queue name=q2 {Q} ! videoconvert n-threads={hilos}"
             f" ! video/x-raw,format=UYVY ! queue name=q3 {Q}")
    if not ndi:
        # solo al HDMI (el multiview del ATEM para el operador) o a ninguna parte
        video += (f" ! kmssink name=sink sync=false connector-id={cfg['hdmi']}{alquiler(cfg)}" if cfg.get("hdmi")
                  else " ! fakesink name=sink sync=false async=false")
    elif cfg.get("hdmi"):
        # copia al HDMI como monitor de encuadre: sin sincronizar y con una cola
        # que descarta, para no frenar nunca el NDI. UYVY va a un plano de vídeo
        # de la GPU sin pasar por la CPU. Esos planos no siempre escalan (Cherry
        # Trail no), así que para verla a pantalla completa la pantalla tiene que
        # arrancar a la resolución de la cámara (ajuste "hdmi_consola")
        video += (" ! tee name=t t. ! queue name=qh max-size-buffers=1 leaky=downstream"
                  f" ! kmssink name=hdmi sync=false connector-id={cfg['hdmi']}{alquiler(cfg)}"
                  " t. ! queue name=q4 max-size-buffers=2 leaky=downstream")
    if not cfg.get("audio"):
        return video if not ndi else f"{video} ! ndisink name=sink"
    # Retraso del audio (audio de otra interfaz USB, que llega antes que la imagen
    # de la cámara): se desplaza el tiempo del audio en el pad de 'retraso' (ver
    # fijar_retraso). La cola de entrada aguanta lo que se retrase y medio segundo
    # más. Con baja latencia el combinador no espera por tiempos: ahí el retraso
    # es real, una cola que no suelta nada hasta tener ese tanto. La ganancia, antes
    # de 'af': así le llega también a la salida por GPU
    real = (" ! queue name=qr max-size-time=0 max-size-buffers=0 max-size-bytes=0"
            if cfg.get("baja_latencia") and ndi else "")
    audio = (f"alsasrc name=asrc ! queue name=qa max-size-time={(RETRASO_MAX_MS + 500) * 1000000}"
             " leaky=downstream ! audioconvert ! audioresample ! volume name=vol"
             " ! capsfilter name=af caps=audio/x-raw,format=F32LE,rate=48000"
             " ! identity name=retraso"
             # 'level' mide el nivel (dBFS) cada 200 ms para el medidor del panel
             " ! level name=nivel interval=200000000 post-messages=true" + real)
    if ndi:
        return f"{video} ! c.video {audio} ! c.audio ndisinkcombiner name=c ! ndisink name=sink"
    return f"{video} {audio} ! fakesink name=asink sync=false async=false"


def fijar_ganancia(flujo, db, mudo=False):
    """Ganancia (dB) y silencio del audio; se puede cambiar en marcha."""
    vol = flujo.elemento("vol")
    if vol is not None:
        vol.set_property("volume", 10 ** (max(-60.0, min(24.0, float(db))) / 20))
        vol.set_property("mute", bool(mudo))


def tramo_camara(cfg):
    if cfg["fmt"] == "MJPG":
        return (f"v4l2src name=src ! image/jpeg,width={cfg['ancho']},height={cfg['alto']},"
                f"framerate={cfg['fps']}/1 ! queue name=q1 {Q}")
    if cfg["fmt"] == "H264":
        # lo comprime la propia cámara (la DJI Pocket 3): aquí se decodifica para el
        # NDI y el HDMI; sale en I420, como el JPEG, y sigue igual que con MJPEG
        tramo = (f"v4l2src name=src ! video/x-h264,width={cfg['ancho']},height={cfg['alto']},"
                 f"framerate={cfg['fps']}/1 ! queue name=q1 {Q} ! h264parse ! avdec_h264 name=deco")
    else:
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
    # en vertical (1080x1920) la miniatura también va de pie
    tam = "width=360,height=640" if cfg["alto"] > cfg["ancho"] else "width=640,height=360"
    return (" tp. ! queue max-size-buffers=1 leaky=downstream ! videorate drop-only=true"
            f" ! video/x-raw,framerate=1/2 ! videoscale ! video/x-raw,{tam}"
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


def hdmi_gpu_disponible(cfg):
    """La ruta directa no descarga vídeo para NDI ni reemplaza su decodificador.

    El sumidero privado importa DMA-BUF con modificadores; kmssink 1.24 no
    negocia DMA_DRM. Si no está instalado, se conserva la ruta CPU existente.
    """
    if (cfg.get("fmt") != "MJPG" or not cfg.get("hdmi")
            or cfg.get("audio") or cfg.get("hdmi_gpu_cpu")):
        return False
    try:
        if not all(Gst.ElementFactory.find(n) for n in ("vajpegdec", "vapostproc")):
            return False
        if con_ndi(cfg):
            # Sin el parche de conexiones no se puede dormir el decodificador:
            # conservar entonces el camino convencional, que siempre emite.
            ndi = Gst.ElementFactory.make("ndisink")
            if ndi is None or ndi.find_property("connections") is None:
                return False
        # Cargar siempre la copia instalada, no una entrada de la caché que
        # podría apuntar a un directorio de pruebas ya borrado o de otro usuario.
        ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "gst", "libgstndihdmikms.so")
        if not os.path.isfile(ruta):
            return False
        Gst.Plugin.load_file(ruta)
        return bool(Gst.ElementFactory.find("ndihdmisink"))
    except GLib.Error:
        return False


def tramo_ndi_jpeg(formato="I420"):
    """NDI acepta el I420 nativo de jpegdec; no forzar UYVY sin medirlo."""
    if formato not in ("I420", "NV12", "UYVY"):
        raise ValueError("Formato NDI no admitido")
    conversion = "" if formato == "I420" else " ! videoconvert n-threads=2"
    return f"jpegdec name=ndi_deco{conversion} ! video/x-raw,format={formato}"


class NdiDemanda:
    """Emisor anunciado aun sin vídeo; solo decodifica si hay receptores.

    Solo recibe JPEG comprimido. appsrc y la cola no bloquean al hilo de la
    cámara/HDMI. Un error se recupera en este pipeline, sin tocar la pantalla.
    """

    def __init__(self, cfg):
        self.formato = cfg.get("ndi_formato", "I420")
        self.p = Gst.parse_launch(
            "appsrc name=ndi_ent format=time is-live=true max-bytes=0"
            " max-buffers=2 leaky-type=downstream"
            f" ! queue name=ndi_q {Q} ! {tramo_ndi_jpeg(self.formato)}"
            " ! ndisink name=ndi_sink sync=false async=false enable-last-sample=false")
        self.entrada = self.p.get_by_name("ndi_ent")
        self.sink = self.p.get_by_name("ndi_sink")
        self.sink.set_property("ndi-name", cfg["nombre"])
        self.conexiones, self.enviados, self.descartes = 0, 0, 0
        self.error, self.reintento, self.timer = "", 0.0, None
        self.cerrada = False
        self.p.get_by_name("ndi_q").connect("overrun", self._descarte)
        self.sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._enviado)
        bus = self.p.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._error)

    def _descarte(self, *_):
        self.descartes += 1

    def _enviado(self, *_):
        self.enviados += 1
        return Gst.PadProbeReturn.OK

    def _fallo(self, detalle):
        self.error, self.conexiones = detalle, 0
        self.reintento = time.monotonic() + 3
        self.p.set_state(Gst.State.NULL)
        informar({"aviso": "ndi_rama_fallida", "detalle": detalle})

    def _error(self, bus, msg):
        if not self.cerrada:
            self._fallo(msg.parse_error()[0].message)

    def actualizar(self):
        if self.cerrada:
            return False
        if self.reintento:
            if time.monotonic() < self.reintento:
                return True
            self.reintento, self.error = 0.0, ""
            if self.p.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                self._fallo("arranque")
                return True
        self.conexiones = int(self.sink.get_property("connections"))
        return True

    def arrancar(self, origen):
        self.p.use_clock(origen.get_clock() or Gst.SystemClock.obtain())
        self.p.set_start_time(Gst.CLOCK_TIME_NONE)
        self.p.set_base_time(origen.get_base_time())
        if self.p.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._fallo("arranque")
        self.timer = GLib.timeout_add(250, self.actualizar)

    def poner_caps(self, caps):
        self.entrada.set_property("caps", caps)

    def jpeg(self, buf):
        if self.conexiones > 0 and not self.error and not self.cerrada:
            # No esperar al decodificador ni al envío de red.
            self.entrada.emit("push-buffer", buf)

    def parar(self):
        self.cerrada = True
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        self.p.set_state(Gst.State.NULL)
        self.p.get_bus().remove_signal_watch()


class HdmiGPU(Reparto):
    """MJPEG -> VA -> YUYV DMA-BUF -> KMS, sin mapear fotogramas crudos.

    Solo el JPEG comprimido cruza appsink/appsrc. El primer JPEG determina
    el submuestreo real; no se presupone el de una miniatura ni otra cámara.
    Se comparte reloj como en Reparto, con colas acotadas para baja latencia.
    Los fallos se etiquetan hdmi_gpu para reintentar con CPU y conservar HDMI.
    """

    # justo después de cambiar de modo, la cámara puede mandar algún fotograma a
    # medias (la DJI Pocket 3 al pasar de 4K a 1080p): se descartan unos cuantos
    # antes de dar la GPU por fallida y pasar a la CPU
    DESCARTES_INICIO = 30

    def __init__(self, cfg):
        self.cfg, self.n, self.caps, self.sin_cabecera = cfg, 1, None, 0
        self.captura = Gst.parse_launch(
            f"{tramo_camara(cfg)} ! appsink name=cap sync=false emit-signals=true"
            " max-buffers=2 drop=true")
        self.salida = Gst.parse_launch(
            "appsrc name=ent format=time is-live=true max-bytes=0 max-buffers=2"
            " leaky-type=downstream ! vajpegdec name=deco_hdmi"
            " ! vapostproc name=vpp_hdmi"
            " ! video/x-raw(memory:DMABuf),format=DMA_DRM,"
            "drm-format=YUYV:0x0100000000000002,colorimetry=bt709"
            f" ! queue name=q2 {Q} ! queue name=q3 {Q}"
            f" ! ndihdmisink name=sink sync=false connector-id={cfg['hdmi']}{alquiler(cfg)}")
        self.entrada = self.salida.get_by_name("ent")
        self.ndi = NdiDemanda(cfg) if con_ndi(cfg) else None
        self.pipelines = [self.salida, self.captura]
        self.captura.get_by_name("cap").connect("new-sample", self._capturado)

    def _capturado(self, sink):
        muestra = sink.emit("pull-sample")
        if muestra is None:
            return Gst.FlowReturn.EOS
        buf = muestra.get_buffer()
        if self.caps is None:
            ok, mapa = buf.map(Gst.MapFlags.READ)
            try:
                cab = cabecera_jpeg(bytes(mapa.data)) if ok else None
            finally:
                if ok:
                    buf.unmap(mapa)
            if cab is None:
                self.sin_cabecera += 1
                return Gst.FlowReturn.OK if self.sin_cabecera < self.DESCARTES_INICIO else Gst.FlowReturn.ERROR
            # el tamaño, el del JPEG que llega de verdad
            muestreo, ancho, alto = cab
            c = self.cfg
            self.caps = Gst.Caps.from_string(
                f"image/jpeg,parsed=true,width={ancho},height={alto},"
                f"framerate={c['fps']}/1,sof-marker=0,colorspace=sYUV,"
                f"sampling={muestreo},interlace-mode=progressive")
            self.entrada.set_property("caps", self.caps)
            if self.ndi is not None:
                self.ndi.poner_caps(self.caps)
            informar({"hdmi_gpu_estado": "activa", "muestreo": muestreo})
        if self.ndi is not None:
            self.ndi.jpeg(buf)
        return self.entrada.emit("push-buffer", buf)

    def elemento(self, nombre):
        if nombre == "ndi_sink" and self.ndi is not None:
            return self.ndi.sink
        return super().elemento(nombre)

    def arrancar(self):
        ok = super().arrancar()
        if ok and self.ndi is not None:
            self.ndi.arrancar(self.captura)
        return ok

    def parar(self):
        if self.ndi is not None:
            self.ndi.parar()
        super().parar()


class DecoGPU(Reparto):
    """El JPEG de la cámara lo decodifica la GPU (vajpegdec) y su procesador de
    vídeo lo deja en UYVY (vapostproc): la CPU solo baja la imagen a memoria para
    el NDI. Medido en un i3-8145U a 1080p: entre 1,7 y 4 veces menos CPU que
    jpegdec, más cuanto más detallada es la imagen.

    Mismo esquema que Reparto con un solo decodificador, pero este se crea con el
    primer JPEG: vajpegdec exige el submuestreo en las caps y hay que leerlo de su
    cabecera. Si el JPEG no le vale (progresivo) o la GPU falla, sigue con jpegdec
    sin cortar el NDI y lo avisa."""

    INTENTOS = 30               # fotogramas que se miran antes de rendirse (1 s a 30 fps)

    def __init__(self, cfg):
        self.cfg = cfg
        self.n = 1
        self.seq = self.esperado = 0
        self.orden, self.pendientes = {}, {}
        self.caps = None
        self.lock = threading.Lock()
        self.modo = None            # gpu | cpu, al llegar el primer JPEG válido
        self.intentos = 0
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
            muestreo = muestreo_jpeg(bytes(info.data)) if ok else None
            if ok:
                buf.unmap(info)
            if muestreo:
                self._cambiar("gpu", muestreo)
            elif self.intentos < self.INTENTOS:
                # recién conectada, una cámara (la DJI Pocket 3) puede mandar algún
                # fotograma roto: se tira y se mira el siguiente
                self.intentos += 1
                return Gst.FlowReturn.OK
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
    cab = cabecera_jpeg(datos)
    return cab[0] if cab else None


def cabecera_jpeg(datos):
    """(submuestreo, ancho, alto) de la cabecera SOF0 de un JPEG baseline, o None."""
    if datos[:2] != b"\xff\xd8":
        return None
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
            alto = int.from_bytes(datos[i + 5:i + 7], "big")
            ancho = int.from_bytes(datos[i + 7:i + 9], "big")
            if datos[i + 9] == 1:
                return "GRAYSCALE", ancho, alto
            h, v = datos[i + 11] >> 4, datos[i + 11] & 15     # factores de la luma
            muestreo = {(2, 2): "YCbCr-4:2:0", (2, 1): "YCbCr-4:2:2",
                        (1, 1): "YCbCr-4:4:4"}.get((h, v))
            return (muestreo, ancho, alto) if muestreo and ancho and alto else None
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
    SEGMENTO = SEGMENTO_S       # duración de cada fichero de grabación, en segundos

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
        self.retraso_ns = 0         # retraso del audio (ver fijar_retraso)
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
                self.primer_jpeg = bytes(mapa.data)
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
                b = info.get_buffer().copy_deep()
                if self.retraso_ns and b.pts != Gst.CLOCK_TIME_NONE:
                    b.pts += self.retraso_ns    # el mismo retraso que en el NDI
                ga.emit("push-buffer", b)
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
        if self.audio and self.caps_audio:
            a = (f" appsrc name=ga format=time is-live=true caps=\"{self.caps_audio}\""
                 " ! queue max-size-time=1000000000 leaky=downstream ! audioconvert ! audioresample"
                 " ! audio/x-raw,rate=48000,channels=2 ! voaacenc bitrate=128000 ! aacparse"
                 f" ! queue ! {pad_a}")
        else:
            # sin micrófono, silencio: lo que se publica lleva siempre pista de audio.
            # Quien lo lea (un relevo que graba o reenvía, OBS, la TV) no tiene que
            # adivinar si vendrá, y un multiplexor que la espera no se queda colgado
            a = (" audiotestsrc name=ga_sil wave=silence is-live=true"
                 " ! audio/x-raw,format=S16LE,rate=48000,channels=2"
                 f" ! voaacenc bitrate=64000 ! aacparse ! queue ! {pad_a}")
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
        liberar_disco(self.g["dir"], kbps=self.g.get("kbps"))

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
        sil = p.get_by_name("ga_sil")
        if sil is not None:
            sil.send_event(Gst.Event.new_eos())
        p.get_bus().timed_pop_filtered(3 * Gst.SECOND,
                                       Gst.MessageType.EOS | Gst.MessageType.ERROR)
        p.set_state(Gst.State.NULL)


def main():
    cfg = leer_cfg()
    Gst.init(None)
    deco = cfg.get("decodificadores") or 1
    if hdmi_gpu_disponible(cfg):
        informar({"hdmi_gpu_estado": "iniciando"})
        try:
            flujo = HdmiGPU(cfg)
        except GLib.Error as ex:
            informar({"aviso": "hdmi_gpu_fallida", "detalle": str(ex)})
            flujo = Directo(cfg)
    elif cfg["fmt"] == "MJPG" and deco == "gpu":
        flujo = DecoGPU(cfg)
    elif cfg["fmt"] == "MJPG" and int(deco) > 1:
        flujo = Reparto(cfg, int(deco))
    else:
        flujo = Directo(cfg)
    flujo.elemento("src").set_property("device", cfg["dev"])
    if con_ndi(cfg):
        ndi_sink = flujo.elemento("ndi_sink") or flujo.elemento("sink")
        ndi_sink.set_property("ndi-name", cfg["nombre"])
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
    retraso = fijar_retraso(flujo, cfg.get("retraso_audio") or 0)
    fijar_ganancia(flujo, cfg.get("ganancia") or 0, cfg.get("mudo"))

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
        # el audio se cuenta donde acaba: en el combinador NDI o, sin NDI, en su sumidero
        destino = flujo.elemento("c").get_static_pad("audio") if con_ndi(cfg) else \
            flujo.elemento("asink").get_static_pad("sink")
        destino.add_probe(Gst.PadProbeType.BUFFER, contar("aud"))
    for cola in ("q1", "q2", "q3"):
        flujo.elemento(cola).connect("overrun", descartado)

    if cfg.get("preview"):
        os.makedirs(os.path.dirname(cfg["preview"]), exist_ok=True)
    if cfg.get("preview") and cfg["fmt"] == "MJPG":
        flujo.elemento("src").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, guardar_preview(cfg["preview"]))

    salidas = []
    sondas = {}         # salida -> [(pad, id de sonda)], para poder desmontarla
    # el pipeline que tiene la cámara marca el reloj y la hora base
    origen = flujo.captura if isinstance(flujo, Reparto) else flujo.p

    def montar_salida(g=None, principal=True):
        """Una salida H.264 (la principal, cfg["gpu"], o una adicional g) colgada
        de la cámara con sondas: se puede añadir y quitar con la cámara en marcha,
        sin cortar el NDI (una ruta que añade OBS o la grabación)."""
        s = SalidaGPU(cfg, origen, bool(cfg.get("audio")), None if principal else g)
        s.retraso_ns = retraso
        pad = flujo.elemento("src").get_static_pad("src")
        ids = [(pad, pad.add_probe(Gst.PadProbeType.BUFFER, s.jpeg))]
        if cfg.get("audio"):
            pad = flujo.elemento("af").get_static_pad("src")
            ids.append((pad, pad.add_probe(Gst.PadProbeType.BUFFER, s.sonido)))
        sondas[s] = ids
        salidas.append(s)
        return s

    def desmontar_salida(s):
        for pad, i in sondas.pop(s, []):
            pad.remove_probe(i)
        salidas.remove(s)
        s.parar()

    if cfg.get("gpu") and cfg["fmt"] == "MJPG":
        montar_salida()
        # en un equipo potente, más salidas a la vez, cada una por su lado
        for g in cfg.get("gpu_extra") or []:
            montar_salida(g, principal=False)

    bucle = GLib.MainLoop()
    salida = [0]

    def mensaje(bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            origen = msg.src.get_name()
            # que la cámara deje de dar imagen (desenchufada, -71…) no es culpa de
            # la GPU: si se etiquetara hdmi_gpu, quedaría vetada hasta reiniciar
            informar({"error": err.message,
                      "elemento": "hdmi_gpu" if isinstance(flujo, HdmiGPU) and origen != "src"
                      else origen})
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
    sink = flujo.elemento("ndi_sink") or flujo.elemento("sink")
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
        if isinstance(flujo, HdmiGPU):
            extra["hdmi_gpu"] = True
            if flujo.ndi is not None:
                extra.update(ndi_out=flujo.ndi.enviados, ndi_desc=flujo.ndi.descartes,
                             ndi_activo=flujo.ndi.conexiones > 0,
                             ndi_formato=flujo.ndi.formato, ndi_error=flujo.ndi.error)
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
        if "retraso_audio" in d:
            # se ajusta en marcha, sin cortar: el oído decide
            nonlocal retraso
            retraso = fijar_retraso(flujo, d["retraso_audio"])
            for s in salidas:
                s.retraso_ns = retraso
        if "ganancia" in d or "mudo" in d:
            fijar_ganancia(flujo, d.get("ganancia", 0), d.get("mudo", False))
        if "calibrar" in d:
            calibrar(flujo, float(d["calibrar"]))
        if "gpu_activa" in d:
            # la orden es para la salida de ese tipo (solo grabar tiene condición)
            for s in salidas:
                if d.get("modo") in (None, s.g["modo"]):
                    s.habilitar(bool(d["gpu_activa"]))
        if "gpu" in d and cfg["fmt"] == "MJPG":
            # otra salida principal (o ninguna), en marcha: la de antes se cierra
            # limpia y la nueva se cuelga de la cámara, sin tocar el NDI
            actual = next((s for s in salidas if s.principal), None)
            if actual is not None and actual.g != d["gpu"]:
                desmontar_salida(actual)
                actual = None
            if d["gpu"] and actual is None:
                cfg["gpu"] = d["gpu"]
                montar_salida()
            elif not d["gpu"]:
                cfg["gpu"] = None
                informar({"gpu_estado": None})
        return True
    GLib.io_add_watch(sys.stdin, GLib.IO_IN | GLib.IO_HUP, orden)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, parar)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, parar)

    if not flujo.arrancar():
        informar({"error": "arranque",
                  "elemento": "hdmi_gpu" if isinstance(flujo, HdmiGPU) else "pipeline"})
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
