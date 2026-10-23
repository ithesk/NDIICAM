# NDIICAM — procesos de cámara, receptor y pantalla de ayuda, y quién usa el HDMI.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time

import nucleo
from nucleo import (AP_IF, AP_IP, FUENTE_PY, GRABACIONES, MAX_DECODIFICADORES, PUERTO_RTMP,
    MIRILLA_PY, MIRILLA_TRAS, NOMBRE_NDI_PUENTE, PROGRAMA_PY, PUERTO_RTMP_LOCAL, RECEPTOR_PY,
    REEMISION_PY, RELEVO_PY,
    REPOSO_PY, RESOLUCIONES, RUTA_PROGRAMA, SALIDAS_EXTRA, VERSION, conexiones_por_pid,
    dispositivo_por_defecto, evento, ip_de, ips_propias, leer)
from camaras import audio_dispositivos, audio_hdmi, camaras, pantallas, resolucion_pantalla
from gpu import capacidad_gpu, capacidad_vpp, decodificador_h264, edicion, entorno_va, simultaneo
import rutas as rutas_mod
from puente import PUENTE, clave_puente, conf_mediamtx_rutas, usuarios_rutas


def lanzar(script, obj, env=None):
    """Arranca un proceso hijo y le pasa su configuración por la entrada estándar
    (una línea JSON): así las claves de emisión y de ingesta no quedan a la vista
    en la lista de procesos. Las órdenes posteriores van por el mismo canal."""
    fd = None
    if obj.get("hdmi") and nucleo.PANTALLAS is not None:
        # la pantalla, alquilada por el servicio (pantallas.py): el hijo hereda el
        # descriptor y pinta solo en ella; si no se puede alquilar, abre la tarjeta él
        fd = nucleo.PANTALLAS.alquilar(obj["hdmi"])
        if fd is not None:
            obj = {**obj, "drm_fd": fd}
    proc = subprocess.Popen([sys.executable, script], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, env=env,
                            pass_fds=(fd,) if fd is not None else ())
    if fd is not None:
        os.close(fd)        # el alquiler vive en el hijo; esta copia sobra
    try:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()
    except OSError:
        pass
    return proc


def tamano_pantalla(monitor):
    """[ancho, alto] reales de esa pantalla (lo sabe la tarjeta) o, si no, lo que
    se pidió al arrancar."""
    t = nucleo.PANTALLAS.resolucion(monitor["id"]) if nucleo.PANTALLAS and monitor else None
    return t or resolucion_pantalla(monitor)


def terminar(proc, espera=8):
    """Pide al proceso que acabe (con SIGTERM los hijos cierran limpio, con EOS, el
    fichero en curso) y lo remata pasada la espera si no lo hace, sin bloquear a
    quien llama: parar un relevo no puede tener al supervisor 8 s quieto. Devuelve
    el proceso, por si hay que esperarlo (ver esperar_fin)."""
    if proc is None or proc.poll() is not None:
        return proc
    try:
        if proc.stdin:
            proc.stdin.close()      # el hijo ve el fin de las órdenes
    except OSError:
        pass
    proc.terminate()

    def rematar():
        try:
            proc.wait(espera)
        except subprocess.TimeoutExpired:
            proc.kill()
    threading.Thread(target=rematar, daemon=True).start()
    return proc


def esperar_fin(procs, segundos=5):
    """Espera, como mucho 'segundos' en total, a que mueran: antes de arrancar otro
    proceso sobre la misma cámara, el mismo ALSA o el HDMI, que son de uno solo."""
    limite = time.monotonic() + segundos
    for p in procs:
        if p is None:
            continue
        try:
            p.wait(max(0.0, limite - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass


class Fuente:
    """Una cámara emitida por NDI desde un proceso fuente.py."""

    def __init__(self, cam):
        self.cam = cam
        self.proc = None
        self.obj = None             # dict con lo que corre: dev, ancho, alto, fps, fmt, nombre, audio
        self.estado = "iniciando"   # iniciando | emitiendo | error | desactivada
        self.error = ""
        self.desde = None
        self.reintento = 0.0
        self.sin_flujo = None
        self.forzar_reinicio = False
        self.audio_fallido = None   # dispositivo de audio que hizo caer el pipeline
        self.hilos = 1              # decodificadores JPEG en modo automático
        self.deco_gpu_fallida = False   # la GPU no pudo con su JPEG: seguir por CPU
        self.calibracion = None     # última medida del desfase audio/imagen (palmadas)
        self.retrasos = 0           # mediciones seguidas con la emisión por detrás de la cámara
        self.hdmi_fallido = False   # el HDMI hizo caer el pipeline: seguir sin él
        self.gpu = {"estado": None, "error": ""}   # salida H.264: activa | error
        self.gpu_extra = {}         # salidas H.264 adicionales (equipo potente): modo -> estado
        self.tally = None           # {"programa": bool, "previo": bool} si el plugin lo da
        self.c = {"cam": 0, "bytes": 0, "out": 0, "desc": 0, "aud": 0}
        self.prev = None
        self.stats = {"fps_camara": 0, "fps_enviados": 0, "mbps_camara": 0,
                      "descartes": 0, "audio_activo": False, "audio_db": None,
                      "latencia_ms": None, "fps_gpu": 0, "mbps_gpu": 0}

    def arrancar(self, obj):
        self.obj = obj
        self.estado, self.error, self.sin_flujo = "iniciando", "", None
        self.tally = None
        self.c = {}
        self.prev = None
        self.gpu = {"estado": "iniciando" if obj.get("gpu") else None, "error": ""}
        self.gpu_extra = {g["modo"]: {"estado": "iniciando", "error": ""}
                          for g in obj.get("gpu_extra") or []}
        self.gpu_pedida = None      # última orden de grabar (True/False) enviada al proceso
        self.proc = lanzar(FUENTE_PY, obj, env=entorno_va())
        threading.Thread(target=self._leer, args=(self.proc,), daemon=True).start()

    def orden(self, d):
        """Una orden al proceso de la fuente (JSON por línea)."""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write(json.dumps(d) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def _leer(self, proc):
        for linea in proc.stdout:
            try:
                d = json.loads(linea)
            except ValueError:
                continue
            if proc is not self.proc:
                break               # un informe tardío del proceso anterior no es de este
            if "gpu_error" in d:
                # la salida GPU va aparte: el NDI sigue y fuente.py la reintenta
                if self.gpu["error"] != d["gpu_error"]:
                    evento("gpu_fallo", fuente=self.cam["nombre"], detalle=d["gpu_error"])
                self.gpu = {"estado": "error", "error": d["gpu_error"]}
            elif "gpu_estado" in d:
                if self.gpu["estado"] != d["gpu_estado"] and d["gpu_estado"] == "activa":
                    evento("gpu_activa", fuente=self.cam["nombre"],
                           modo=((self.obj or {}).get("gpu") or {}).get("modo"))
                self.gpu = {"estado": d["gpu_estado"], "error": ""}
            elif "gpu_extra" in d:
                # una salida adicional: como la principal, falla y se reintenta sola
                modo = d["gpu_extra"]
                antes = self.gpu_extra.get(modo, {})
                if d.get("error") and antes.get("error") != d["error"]:
                    evento("gpu_fallo", fuente=self.cam["nombre"], detalle=d["error"], modo=modo)
                elif d.get("estado") == "activa" and antes.get("estado") != "activa":
                    evento("gpu_activa", fuente=self.cam["nombre"], modo=modo)
                self.gpu_extra[modo] = {"estado": "error" if d.get("error") else d.get("estado"),
                                        "error": d.get("error", "")}
            elif "error" in d:
                self._fallo(d["error"], d.get("elemento", ""))
            elif "calibracion" in d:
                self.calibracion = {**d["calibracion"], "estado": "hecha", "t": time.time()}
                evento("calibracion", fuente=self.cam["nombre"], **d["calibracion"])
            elif "aviso" in d:
                if d["aviso"] == "deco_gpu_fallida" and not self.deco_gpu_fallida:
                    # fuente.py ya siguió por CPU. Si falló la GPU, los próximos arranques
                    # también; si fue el JPEG (fotogramas raros), se vuelve a probar
                    self.deco_gpu_fallida = d.get("detalle") != "jpeg_no_admitido"
                    evento("deco_gpu_fallida", fuente=self.cam["nombre"], detalle=d.get("detalle"))
            else:
                self.c = d
                if "tally_programa" in d:
                    tally = {"programa": d["tally_programa"], "previo": d["tally_previo"]}
                    if self.tally is not None and tally != self.tally:
                        evento("tally", fuente=self.cam["nombre"], **tally)
                    self.tally = tally
        proc.wait()
        if proc is self.proc and self.estado in ("iniciando", "emitiendo"):
            self._fallo("proceso", "")

    def _fallo(self, error, elemento, espera=3):
        if self.proc is None:
            return
        if self.obj and self.obj.get("hdmi") and error not in ("sin_imagen", "proceso") and (
                self.estado == "iniciando" or elemento in ("hdmi", "qh", "t")
                or "kms" in error.lower()):
            # con HDMI, un fallo de arranque se achaca al HDMI aunque lo reporte
            # otro elemento (un not-negotiated sale por la cámara): mejor seguir sin
            # él que dejar el NDI caído en bucle. Que la cámara no dé imagen no es
            # cosa del HDMI
            self.hdmi_fallido = True
            evento("hdmi_fallo", fuente=self.cam["nombre"], detalle=error)
            espera = 1
        elif self.obj and self.obj.get("audio") and (
                elemento.startswith("asrc") or "alsa" in error.lower()):
            # el audio hizo caer el pipeline: seguir sin él
            self.audio_fallido = self.obj["audio"]
            evento("audio_fallo", fuente=self.cam["nombre"], detalle=error)
            espera = 1
        else:
            evento("error_emision", fuente=self.cam["nombre"], detalle=error)
        self.detener()
        self.estado, self.error = "error", error
        self.reintento = time.time() + espera

    def detener(self):
        proc, self.proc = self.proc, None
        self.obj = self.desde = None
        return terminar(proc, 5)

    def medir(self, t):
        """Tasas desde la última medición; detecta la pérdida de imagen."""
        c = dict(self.c)
        if "t" not in c:
            return                  # aún no ha llegado ningún informe
        if self.prev and self.proc is not None and c["t"] > self.prev[1]["t"]:
            p = self.prev[1]
            dt = c["t"] - p["t"]
            fps_out = (c["out"] - p["out"]) / dt
            self.stats = {"fps_camara": round((c["cam"] - p["cam"]) / dt, 1),
                          "fps_enviados": round(fps_out, 1),
                          "mbps_camara": round((c["bytes"] - p["bytes"]) * 8 / dt / 1e6, 1),
                          "descartes": c["desc"] - p["desc"],
                          "audio_activo": c["aud"] > p["aud"],
                          "audio_db": {"peak": c.get("db_peak"), "rms": c.get("db_rms")},
                          "latencia_ms": c.get("lat_ms"),
                          "fps_gpu": round((c.get("gpu", 0) - p.get("gpu", 0)) / dt, 1),
                          "mbps_gpu": round((c.get("gpu_bytes", 0) - p.get("gpu_bytes", 0))
                                            * 8 / dt / 1e6, 1)}
            if fps_out > 0.5:
                self.sin_flujo = None
                # la cámara entrega más de lo que sale y hay núcleos libres: la
                # decodificación no da abasto; repartirla en un hilo más
                fps_cam = self.stats["fps_camara"]
                self.retrasos = self.retrasos + 1 if fps_cam >= 20 and fps_cam - fps_out >= 3 else 0
                if (self.retrasos >= 3 and self.obj.get("fmt") == "MJPG"
                        and self.obj.get("decodificadores", 1) == self.hilos
                        and self.hilos < MAX_DECODIFICADORES):
                    self.hilos += 1
                    self.retrasos = 0
                    evento("decodificacion_paralela", fuente=self.cam["nombre"], hilos=self.hilos)
                if self.estado != "emitiendo":
                    self.estado, self.desde = "emitiendo", t
                    evento("emitiendo", fuente=self.cam["nombre"], nombre=self.obj["nombre"],
                           modo=f"{self.obj['ancho']}×{self.obj['alto']} @{self.obj['fps']}",
                           formato=self.obj["fmt"], audio=bool(self.obj.get("audio")))
            elif self.sin_flujo is None:
                self.sin_flujo = t
            elif t - self.sin_flujo > 12:
                evento("sin_imagen", fuente=self.cam["nombre"])
                self._fallo("sin_imagen", "src", espera=2)
        elif self.proc is not None and self.prev and t - self.prev[0] > 15:
            # el proceso no informa: colgado
            evento("sin_imagen", fuente=self.cam["nombre"])
            self._fallo("sin_imagen", "src", espera=2)
            return
        if not self.prev or c["t"] > self.prev[1]["t"]:
            self.prev = (t, c)

class BuscadorNDI:
    """Fuentes NDI de la red, sin las de este equipo. Usa el monitor de
    dispositivos del plugin NDI de GStreamer, que arranca la primera vez que
    se le pide la lista: un equipo que no usa el receptor no busca nada."""

    def __init__(self):
        self.lock = threading.Lock()
        self.monitor = None
        self.fallo = None
        self.cache = (0.0, [])

    def _iniciar(self):
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
            m = Gst.DeviceMonitor.new()
            m.add_filter("Source/Network", None)
            if not m.start():
                raise RuntimeError("DeviceMonitor")
            self.monitor = m
        except Exception as ex:
            self.fallo = str(ex)
            evento("fallo_interno", detalle=f"NDI finder: {ex}")

    def lista(self):
        with self.lock:
            if self.monitor is None and self.fallo is None:
                self._iniciar()
            if self.monitor is None:
                return []
            if time.time() - self.cache[0] < 3:
                return self.cache[1]
            devs = self.monitor.get_devices()
            propias, res = ips_propias(), []
            for d in devs:
                props = d.get_properties()
                if not props or not props.has_field("ndi-name"):
                    continue
                url = props.get_string("url-address") or ""
                if url.rsplit(":", 1)[0] in propias:
                    continue
                res.append({"nombre": props.get_string("ndi-name"), "url": url})
            res.sort(key=lambda x: x["nombre"].lower())
            self.cache = (time.time(), res)
            return res

class Reposo:
    """La pantalla de ayuda (reposo.py) en el HDMI mientras nadie lo usa."""

    def __init__(self):
        self.proc = None
        self.obj = None
        self.reintento = 0.0

    def arrancar(self, obj):
        self.obj = obj
        self.proc = lanzar(REPOSO_PY, obj)
        threading.Thread(target=self._leer, args=(self.proc,), daemon=True).start()

    def _leer(self, proc):
        for linea in proc.stdout:
            try:
                d = json.loads(linea)
            except ValueError:
                continue
            if "error" in d and proc is self.proc:
                evento("reposo_fallo", detalle=d["error"])
        proc.wait()
        if proc is self.proc:
            # murió solo: no insistir en bucle
            self.proc, self.obj = None, None
            self.reintento = time.time() + 15

    def detener(self):
        proc, self.proc = self.proc, None
        self.obj = None
        return terminar(proc, 5)

class Receptor:
    """Una fuente NDI de la red mostrada en el HDMI desde un proceso receptor.py."""

    def __init__(self):
        self.proc = None
        self.obj = None             # dict con lo que corre: nombre, hdmi, audio, equipo
        self.destino = None         # fuente pedida (para reiniciar los reintentos al cambiarla)
        self.estado = "apagado"     # apagado | conectando | recibiendo | error
        self.error = ""
        self.desde = None
        self.reintento = 0.0
        self.fallos = 0
        self.audio_fallido = None
        self.plano_fallido = False  # el plano de vídeo no escala: escalar por CPU
        self.gpu_fallida = False    # la GPU no pudo decodificar el H.264: por CPU
        self.c = {}
        self.prev = None
        self.stats = {"fps": 0, "audio_activo": False, "audio_db": None, "latencia_ms": None}

    def arrancar(self, obj):
        self.obj = obj
        self.estado, self.error = "conectando", ""
        self.c, self.prev = {}, None
        self.stats = {"fps": 0, "audio_activo": False, "audio_db": None, "latencia_ms": None}
        self.proc = lanzar(RECEPTOR_PY, obj, env=entorno_va())
        threading.Thread(target=self._leer, args=(self.proc,), daemon=True).start()

    def _leer(self, proc):
        for linea in proc.stdout:
            try:
                d = json.loads(linea)
            except ValueError:
                continue
            if proc is not self.proc:
                break
            if "error" in d:
                self._fallo(d["error"], d.get("elemento", ""))
            else:
                self.c = d
        proc.wait()
        if proc is self.proc and self.estado in ("conectando", "recibiendo"):
            self._fallo("proceso", "")

    def _fallo(self, error, elemento):
        if self.proc is None:
            return
        # el objeto puede haberse soltado ya (detener desde otro hilo): copia segura
        obj = self.obj or {}
        nombre = obj.get("nombre", "")
        puente = obj.get("origen") == "puente"
        if error == "eos" or "demultiplex" in error.lower() or (
                puente and (error in ("proceso", "arranque") or "rtmp" in error.lower()
                            or "connect" in error.lower() or "stream" in error.lower())):
            error = "sin_senal"     # así avisa ndisrc de que la fuente dejó de emitir
        if error == "plano_sin_escala":
            # se vuelve a arrancar enseguida escalando por CPU (dura lo que dure el servicio)
            self.plano_fallido = True
            evento("rx_plano_sin_escala", fuente=nombre)
            espera = 1
        elif (obj.get("gpu_h264") and elemento in ("dec", "vpp")
              or obj.get("gpu_h264") and self.estado == "conectando"
              and "va" in error.lower()):
            self.gpu_fallida = True
            evento("rx_gpu_fallida", fuente=nombre, detalle=error)
            espera = 1
        elif obj.get("audio") and (elemento in ("asink", "qa") or "alsa" in error.lower()):
            # el audio HDMI hizo caer el pipeline: seguir sin él
            self.audio_fallido = obj["audio"]
            evento("rx_audio_fallo", fuente=nombre, detalle=error)
            espera = 1
        else:
            # la fuente puede tardar en aparecer o haberse ido: reintentar cada
            # vez más espaciado, avisando solo del primer fallo
            if self.estado == "recibiendo":
                evento("rx_perdida", fuente=nombre)
            elif self.fallos == 0:
                evento("rx_error", fuente=nombre, detalle=error)
            self.fallos += 1
            espera = 2 if puente else min(15, 3 * self.fallos)
        self.detener()
        self.estado, self.error = "error", error
        self.ultimo_error = error     # sobrevive al reintento: el panel explica por qué no hay imagen
        self.reintento = time.time() + espera

    def detener(self):
        proc, self.proc = self.proc, None
        self.obj = self.desde = None
        return terminar(proc, 5)

    def medir(self, t):
        """Tasas desde el último informe. A diferencia de una cámara, que no
        lleguen imágenes no es un fallo: un emisor NDI con la imagen quieta
        puede no mandar ninguna más, y si desaparece ndisrc da error solo."""
        c = dict(self.c)
        if self.proc is None:
            return
        if "t" not in c or (self.prev and c["t"] <= self.prev[1]["t"]):
            if self.prev and t - self.prev[0] > 15:
                self._fallo("proceso", "")   # no informa: colgado
            return
        if self.prev:
            p = self.prev[1]
            dt = c["t"] - p["t"]
            self.stats = {"fps": round((c["rx"] - p["rx"]) / dt, 1),
                          "audio_activo": c["aud"] > p["aud"],
                          "audio_db": {"peak": c.get("db_peak"), "rms": c.get("db_rms")},
                          "latencia_ms": c.get("lat_ms")}
        if c["rx"] > 0 and self.estado != "recibiendo":
            self.estado, self.desde, self.fallos = "recibiendo", t, 0
            fps = c.get("fps")
            evento("rx_recibiendo", fuente=self.obj["nombre"],
                   modo=f"{c.get('ancho')}×{c.get('alto')}" + (f" @{fps:g}" if fps else ""))
        self.prev = (t, c)

SIN_AVANCE = 8      # s activo sin sacar fotogramas: el auxiliar se da por colgado


class Auxiliar:
    """Un proceso de la edición Pro que no es una cámara ni el receptor: la
    reemisión de una fuente NDI (reemision.py) o el programa final del puente
    (programa.py). Si falla, se reintenta cada vez más espaciado. 'prefijo' da
    nombre a sus eventos (reem_activa, programa_error...)."""

    def __init__(self, script, prefijo, callado=False):
        self.script, self.prefijo = script, prefijo
        self.callado = callado      # sin eventos en la actividad (la mirilla va y viene sin más)
        self.proc = None
        self.obj = None
        self.estado = "apagado"     # apagado | conectando | activa | error
        self.error = ""
        self.desde = None
        self.reintento = 0.0
        self.fallos = 0
        self.c = {}
        self.prev = None
        self.parado = None          # desde cuándo está activo sin sacar nada
        self.stats = {"fps": 0, "mbps": 0, "audio_activo": False}

    def arrancar(self, obj):
        self.obj = obj
        self.estado, self.error = "conectando", ""
        self.c, self.prev, self.parado = {}, None, None
        self.stats = {"fps": 0, "mbps": 0, "audio_activo": False}
        self.proc = lanzar(self.script, obj, env=entorno_va())
        threading.Thread(target=self._leer, args=(self.proc,), daemon=True).start()

    def orden(self, d):
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write(json.dumps(d) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def _leer(self, proc):
        for linea in proc.stdout:
            try:
                d = json.loads(linea)
            except ValueError:
                continue
            if proc is not self.proc:
                break
            if "error" in d:
                self._fallo(d["error"])
            else:
                self.c = d
        proc.wait()
        if proc is self.proc and self.estado in ("conectando", "activa"):
            self._fallo("proceso")

    def _fallo(self, error):
        if self.proc is None:
            return
        if error == "eos" or "demultiplex" in error.lower():
            error = "sin_senal"
        if (self.estado == "activa" or self.fallos == 0) and not self.callado:
            evento(f"{self.prefijo}_error", fuente=self.obj.get("nombre", ""), detalle=error)
        self.fallos += 1
        self.detener()
        self.estado, self.error = "error", error
        self.ultimo_error = error
        self.reintento = time.time() + min(30, 3 * self.fallos)

    def detener(self):
        proc, self.proc = self.proc, None
        self.obj = self.desde = None
        return terminar(proc, 8)    # 8 s: que cierre el fichero en curso

    def medir(self, t):
        c = dict(self.c)
        if self.proc is None:
            return
        if "t" not in c or (self.prev and c["t"] <= self.prev[1]["t"]):
            if self.prev and t - self.prev[0] > 15:
                self._fallo("proceso")
            return
        if self.prev:
            p = self.prev[1]
            dt = c["t"] - p["t"]
            self.stats = {"fps": round((c["out"] - p["out"]) / dt, 1),
                          "mbps": round((c["bytes"] - p["bytes"]) * 8 / dt / 1e6, 1),
                          "audio_activo": c["aud"] > p["aud"],
                          "audio_db": {"peak": c.get("db_peak"), "rms": c.get("db_rms")},
                          "desfase_ms": c.get("desfase_ms")}
        # vivo pero sin sacar nada (la fuente se fue y volvió y este lector quedó
        # colgado de la conexión vieja): reiniciarlo, porque solo no se recupera
        if self.estado == "activa" and self.prev and c.get("out", 0) == self.prev[1].get("out", 0):
            self.parado = self.parado or t
            if t - self.parado > SIN_AVANCE:
                self.parado = None
                return self._fallo("sin_senal")
        else:
            self.parado = None
        if c.get("out", 0) > 0 and self.estado != "activa":
            self.estado, self.desde, self.fallos = "activa", t, 0
            if not self.callado:
                evento(f"{self.prefijo}_activa", fuente=self.obj.get("nombre", ""),
                   salidas=[s["modo"] for s in self.obj.get("salidas", [])],
                   consola=bool(self.obj.get("audio")), grabar=bool(self.obj.get("grabar")))
        self.prev = (t, c)

class MotorRutas:
    """Las rutas (rutas.py) hechas procesos. Cada paso: entradas detectadas + rutas
    guardadas → plan → arranca lo que falta, para lo que sobra y ajusta en marcha lo
    que se puede (retraso, ganancia, silencio) sin cortar. Los procesos son los de
    siempre: Fuente (cámaras), Auxiliar (programa, reemisión, relevo) y Receptor."""

    # se cambian sin reiniciar: el audio en marcha, y la salida H.264 de una cámara
    # (fuente.py la monta y desmonta sin cortar el NDI)
    VIVOS = ("retraso_audio", "ganancia", "mudo", "gpu")

    def __init__(self, sup):
        self.sup = sup          # Fuentes: config, audio, objetivo() de las cámaras
        self.procs = {}         # clave -> objeto de proceso
        self.lanzado = {}       # clave -> con qué se lanzó por última vez
        self.ultimo = {"procesos": [], "activas": [], "pausadas": [], "mediamtx": {}}
        self.errores = {}       # ruta -> errores; se sustituye entero al final de cada paso
        self._err = {}          # los del paso en curso (validar + lo que no se pudo arrancar)
        self.limpias = []
        self.entradas = []
        self.pantallas_usadas = set()   # conectores que usan (o van a usar) las rutas
        self._usadas = set()
        self.soltar_hdmi = None  # lo pone Fuentes: para la ayuda de esa pantalla antes de arrancar

    def entradas_actuales(self, cams, cfg):
        """Las cámaras e interfaces de audio conectadas, y las entradas de red guardadas."""
        audio = self.sup.audio
        ents = []
        for c in cams.values():
            mic = next((a["dev"] for a in audio if a["usb"] and a["usb"] == c.get("usb")), None)
            ents.append({"id": c["id"], "tipo": "usb_cam", "nombre": c["nombre"], "dev": c["dev"],
                         "audio_dev": mic})
        de_camara = {c.get("usb") for c in cams.values() if c.get("usb")}
        for a in audio:
            if not (a["usb"] and a["usb"] in de_camara):
                ents.append({"id": "audio:" + a["id"], "tipo": "usb_audio", "nombre": a["nombre"],
                             "dev": a["dev"]})
        for e in cfg.get("entradas_red") or []:
            ents.append(dict(e))
        return ents

    def paso(self, cfg, cams, monitores):
        """monitores: las pantallas con monitor, en orden ([{id, conector, modo}])."""
        self.planificar(cfg, cams, [m["conector"] for m in monitores])
        self.aplicar(cfg, cams, {m["conector"]: m for m in monitores})

    def planificar(self, cfg, cams, pantallas=None):
        """Entradas y rutas → plan, sin tocar ningún proceso."""
        self.entradas = self.entradas_actuales(cams, cfg)
        limpias, err = rutas_mod.validar(cfg.get("rutas") or [], self.entradas)
        pl = rutas_mod.plan(limpias, self.entradas, simultaneo(cfg), err, pantallas)
        self.ultimo, self.limpias, self._err = pl, limpias, err
        return pl

    def aplicar(self, cfg, cams, monitores):
        """Arranca lo que falta, para lo que sobra y ajusta en marcha lo que se puede."""
        pl = self.ultimo
        self._usadas = set()
        m = pl["mediamtx"]
        if m["necesario"] and PUENTE.disponible():
            # la conf no cambia con las rutas (ver conf_mediamtx_rutas) y los permisos
            # de las ingestas se aplican por la API: MediaMTX no se reinicia nunca aquí
            PUENTE.arrancar_conf(conf_mediamtx_rutas(m["ingestas"]), usuarios_rutas(m["ingestas"]))
        elif not m["necesario"]:
            PUENTE.detener()
        deseados = {}
        for pr in pl["procesos"]:
            obj = self.objeto(pr, cfg, cams, monitores)
            if obj is not None:
                deseados[pr["clave"]] = (pr, obj)
        self.pantallas_usadas = set(self._usadas)
        # cámaras que ninguna ruta usa: mientras alguien mira el panel, una mirilla
        # por cámara (vista previa sin encender ninguna ruta). Se va sola cuando una
        # ruta necesita la cámara (se para antes de arrancarla) o nadie mira
        if time.time() - nucleo.PANEL_VISTO < MIRILLA_TRAS:
            en_uso = {pr["cam"] for pr, _ in deseados.values() if pr["clase"] == "camara"}
            for cid, cam in cams.items():
                obj = None if cid in en_uso else self.objeto_mirilla(cam)
                if obj:
                    deseados[f"mir:{cid}"] = ({"clave": f"mir:{cid}", "clase": "mirilla", "cam": cid,
                                               "ruta": None, "nombre_ruta": cam["nombre"]}, obj)
        moribundos = []
        for clave in list(self.procs):
            if clave not in deseados:
                moribundos.append(self.procs.pop(clave).detener())
        for clave, (pr, obj) in deseados.items():
            p = self.procs.get(clave)
            if p is None:
                p = self.procs[clave] = self.nuevo(pr, cams)
            if p.proc is not None and p.obj and (p.obj != obj or getattr(p, "forzar_reinicio", False)):
                fijo = {k: v for k, v in obj.items() if k not in self.VIVOS}
                antes = {k: v for k, v in p.obj.items() if k not in self.VIVOS}
                if fijo == antes and not getattr(p, "forzar_reinicio", False):
                    # solo lo que se cambia, en marcha
                    cambios = {k: obj[k] for k in self.VIVOS if k in obj and obj[k] != p.obj.get(k)}
                    p.orden(cambios)
                    if "gpu" in cambios and isinstance(p, Fuente):
                        p.gpu = {"estado": "iniciando" if obj["gpu"] else None, "error": ""}
                    p.obj = obj
                else:
                    moribundos.append(p.detener())
                    evento("aplicando", fuente=pr["nombre_ruta"])
            p.forzar_reinicio = False
            if p.proc is None and self.lanzado.get(clave) not in (None, obj):
                # esperaba para reintentar con lo de antes (una fuente NDI que no manda
                # imagen, por ejemplo) y le han cambiado algo: probar ya con lo nuevo
                p.reintento, p.fallos = 0.0, 0
        arrancables = [(pr, obj) for clave, (pr, obj) in deseados.items()
                       if self.procs[clave].proc is None and time.time() >= self.procs[clave].reintento]
        if arrancables:
            # lo que se paró puede tener la cámara, el ALSA o el HDMI que otro va a
            # coger: que lo suelte antes (acotado). Y la pantalla de ayuda igual
            if self.soltar_hdmi:
                for nombre in {m["conector"] for m in monitores.values()
                               for _, obj in arrancables if obj.get("hdmi") == m["id"]}:
                    moribundos.append(self.soltar_hdmi(nombre))
            esperar_fin(moribundos)
            for pr, obj in arrancables:
                self.procs[pr["clave"]].arrancar(obj)
                self.lanzado[pr["clave"]] = obj
        # los errores, de una vez: el panel los lee desde otro hilo
        self.errores = {k: sorted(set(v)) for k, v in self._err.items() if v}

    def nuevo(self, pr, cams):
        if pr["clase"] == "camara":
            return Fuente(cams[pr["cam"]])
        if pr["clase"] == "mirilla":
            a = Auxiliar(MIRILLA_PY, "mirilla", callado=True)
            a.cam = cams[pr["cam"]]     # el panel busca la vista previa por la cámara
            return a
        if pr["clase"] == "receptor":
            return Receptor()
        return Auxiliar({"programa": PROGRAMA_PY, "reemision": REEMISION_PY,
                         "relevo": RELEVO_PY}[pr["clase"]], pr["clase"])

    def objeto_mirilla(self, cam):
        """La cámara en su modo más pequeño (desde 640 de ancho) y lento: la vista
        previa no necesita más y así ni el USB ni la CPU lo notan."""
        modos = cam.get("modos") or []
        if not modos:
            return None
        mjpg = [m for m in modos if m[3] == "MJPG"] or modos
        grandes = [m for m in mjpg if m[0] >= 640] or mjpg
        ancho, alto = min((m[0] * m[1], m[0], m[1]) for m in grandes)[1:]
        fps = min((m[2] for m in grandes if (m[0], m[1]) == (ancho, alto) and m[2] >= 5),
                  default=min(m[2] for m in grandes if (m[0], m[1]) == (ancho, alto)))
        fmt = next(m[3] for m in grandes if (m[0], m[1], m[2]) == (ancho, alto, fps))
        return {"dev": cam["dev"], "fmt": fmt, "ancho": ancho, "alto": alto, "fps": fps,
                "preview": ruta_preview(cam["id"]), "nombre": cam["nombre"]}

    def objeto(self, pr, cfg, cams, monitores):
        """Lo que se le pasa al proceso: el mismo formato que en los modos. None si
        aún no toca arrancarlo (lo que lee de MediaMTX espera a que alguien publique;
        pero solo para arrancar: el que ya corre sigue aunque la fuente parpadee, y
        es él quien nota el corte y lo dice)."""
        if pr["clase"] == "mirilla":
            return self.objeto_mirilla(cams[pr["cam"]])
        local = PUERTO_RTMP_LOCAL
        a = pr.get("audio") or {}
        con_audio = a.get("de") is not None
        vivos = {"retraso_audio": a.get("retraso", 0) if con_audio else 0,
                 "ganancia": a.get("ganancia", 0), "mudo": bool(a.get("mudo")) or not con_audio}
        # el monitor de esta ruta: el conector que le dio plan() (o el primero, si
        # el plan no distingue pantallas)
        monitor = None
        if pr.get("hdmi"):
            monitor = (monitores.get(pr["hdmi"]) if isinstance(pr["hdmi"], str)
                       else next(iter(monitores.values()), None))
        hdmi = monitor["id"] if monitor else None
        if monitor:
            self._usadas.add(monitor["conector"])
        p = self.procs.get(pr["clave"])
        corriendo = p is not None and p.proc is not None
        if pr["clase"] == "camara":
            # el proceso que ya corre (lo que aprendió: hilos, GPU, qué le falló) o uno nuevo
            f = p or Fuente(cams[pr["cam"]])
            aj = {**self.sup.config.fuente(pr["cam"]), "solo_monitor": False, "gpu_extra": [],
                  "gpu_salida": "rtmp" if pr["publicar"] else "no",
                  "rtmp_url": f"{local}/{pr['publicar']}" if pr["publicar"] else ""}
            obj = self.sup.objetivo(f, aj)
            if pr["publicar"] and not obj["gpu"]:
                self._err.setdefault(pr["ruta"], []).append("sin_gpu")   # no se puede codificar
            dev = a.get("dev") if con_audio else None
            if dev and dev == f.audio_fallido:
                # el audio tumbó el pipeline: sigue sin él, y el panel lo dice
                self._err.setdefault(pr["ruta"], []).append("audio_fallido")
                dev = None
            if hdmi and f.hdmi_fallido:
                self._err.setdefault(pr["ruta"], []).append("hdmi_fallido")
                self._usadas.discard(monitor["conector"])
                hdmi = None
            obj.update(nombre=pr["ndi"] or cams[pr["cam"]]["nombre"], ndi=bool(pr["ndi"]),
                       audio=dev, hdmi=hdmi, solo_monitor=False, preview=ruta_preview(pr["cam"]), **vivos)
            if not obj["audio"]:
                obj.update(retraso_audio=0, baja_latencia=False)
            return obj
        if pr["clase"] == "programa":
            info = PUENTE.ruta_info(pr["entrada"])
            if not corriendo and not info.get("publicando"):
                return None         # el ATEM aún no publica: arrancar ahora sería fallar
            # si lo que llega trae audio se decide al arrancar (cambiarlo reinicia)
            con = p.obj.get("con_audio", True) if corriendo else trae_audio(info)
            return {"nombre": pr["nombre_ruta"], "entrada": f"{local}/{pr['entrada']}",
                    "salida": f"{local}/{pr['publicar']}", "con_audio": con,
                    "audio": a.get("dev") if a.get("de") == "aparte" else None, "grabar": None, **vivos}
        if pr["clase"] == "receptor":
            if pr["origen"] == "rtmp":
                if not corriendo and not PUENTE.ruta_lista(pr["fuente"]):
                    return None         # todavía no publica el programa
                return {"origen": "puente", "nombre": pr["nombre_ruta"], "url": f"{local}/{pr['fuente']}",
                        "decodificador": decodificador_h264(), "hdmi": hdmi,
                        "pantalla": tamano_pantalla(monitor) if hdmi else None,
                        "gpu_h264": capacidad_vpp()["h264"], "ndi_nombre": pr["ndi"],
                        "ndi_audio": True, "audio": audio_hdmi() if hdmi else None}
            if not hdmi:
                return None
            return {"nombre": pr["fuente"], "hdmi": hdmi, "equipo": f"NDIICAM {socket.gethostname()}",
                    "pantalla": tamano_pantalla(monitor), "plano": True, "audio": audio_hdmi()}
        if pr["clase"] == "reemision":
            cap = capacidad_gpu()
            if not cap["disponible"]:
                self._err.setdefault(pr["ruta"], []).append("sin_gpu")
                return None
            return {"nombre": pr["fuente"], "equipo": f"NDIICAM {socket.gethostname()}",
                    "codificador": cap["codificador"], "vpp": capacidad_vpp()["vpp"], "kbps": 8000,
                    "salidas": [{"modo": "rtmp", "url": f"{local}/{pr['publicar']}"}],
                    "audio_dev": a.get("dev") if a.get("de") == "aparte" else None,
                    "sin_audio": not con_audio, **vivos}
        if pr["clase"] == "relevo":
            info = PUENTE.ruta_info(pr["desde"])
            if not corriendo and not info.get("publicando"):
                return None             # aún no hay nada que reenviar o grabar
            obj = {"nombre": pr["nombre_ruta"], "desde": f"{local}/{pr['desde']}", "modo": pr["modo"],
                   "con_audio": p.obj.get("con_audio", True) if corriendo else trae_audio(info)}
            if pr["modo"] == "rtmp":
                obj["url"] = pr["url"]
            else:
                obj.update(dir=GRABACIONES, prefijo=pr["prefijo"], formato=pr["formato"])
            return obj
        return None

    def medir(self, t):
        for p in list(self.procs.values()):
            p.medir(t)

    def detener(self):
        """Para todo y devuelve los procesos, por si hay que esperarlos."""
        procs, self.procs = self.procs, {}
        return [p.detener() for p in procs.values()]

    def reiniciar(self, ident=None):
        """Desde cero una cámara (por id) o todo, olvidando los fallos anteriores
        (HDMI, audio, GPU): el botón Reiniciar del panel."""
        for p in list(self.procs.values()):
            if isinstance(p, Fuente):
                if ident is not None and p.cam["id"] != ident:
                    continue
                p.forzar_reinicio, p.audio_fallido, p.hdmi_fallido = True, None, False
                p.deco_gpu_fallida, p.reintento = False, 0.0
            elif ident is None:
                p.detener()
                p.fallos, p.reintento = 0, 0.0
                if hasattr(p, "audio_fallido"):
                    p.audio_fallido = None

    def calibrar(self, ruta, segundos=8):
        p = self.procs.get(f"cam:{ruta}")
        if p is None or p.proc is None or not (p.obj or {}).get("audio"):
            return False
        p.calibracion = {"estado": "midiendo", "hasta": time.time() + segundos}
        p.orden({"calibrar": segundos})
        return True

    def estado(self):
        """Por ruta: si va, qué le pasa y cómo está cada salida. Se lee desde el
        panel y el medidor mientras paso() cambia los procesos: copias de todo."""
        res = {}
        por_ruta = {}
        for clave, p in list(self.procs.items()):
            por_ruta.setdefault(clave.split(":")[1], []).append((clave, p))
        errores, pausadas = self.errores, list(self.ultimo.get("pausadas", []))
        for r in list(self.limpias):
            rid = r["id"]
            err = errores.get(rid, [])
            if rid in pausadas:
                est = "pausada"
            elif not por_ruta.get(rid):
                est = "esperando"
            else:
                # el estado de la ruta es el de lo que la hace (cámara, programa,
                # reemisión, receptor); un relevo caído (YouTube, el disco) se ve en
                # su salida, no tumba el estado de la ruta
                mios = [p for c, p in por_ruta[rid] if not c.startswith("relevo:")]
                ests = [estado_proceso(p) for p in mios or [p for _, p in por_ruta[rid]]]
                est = "error" if "error" in ests else "en_marcha" if all(e == "ok" for e in ests) else "esperando"
            procs = {c.split(":")[0]: p for c, p in por_ruta.get(rid, [])}
            principal = procs.get("cam") or procs.get("prog") or procs.get("reem") or procs.get("rx")
            # la fuente de red (NDI, ATEM) está anunciada pero no manda imagen: decirlo
            # en vez de un "esperando" sin más (OBS con la fuente apagada, por ejemplo)
            if (principal is not None and getattr(principal, "fallos", 0) > 0
                    and getattr(principal, "ultimo_error", "") in ("sin_senal", "sin_video")):
                err = list(err) + ["fuente_sin_imagen"]
            st = getattr(principal, "stats", {}) if principal else {}
            info = PUENTE.ruta_info(f"live/{r['camino']}") if any(
                s["tipo"] in rutas_mod.DE_RED for s in r["salidas"]) else {}
            salidas = {}
            for s in r["salidas"]:
                if est == "pausada":
                    salidas[s["id"]] = "pausada"
                elif s["tipo"] in ("rtmp", "grabar"):     # su propio relevo
                    p = self.procs.get(f"relevo:{rid}:{s['id']}")
                    salidas[s["id"]] = estado_proceso(p) if p else "esperando"
                elif s["tipo"] in ("obs", "srt"):         # lo sirve MediaMTX
                    salidas[s["id"]] = "ok" if info.get("publicando") else "esperando"
                elif s["tipo"] == "hdmi" and "hdmi_fallido" in err:
                    salidas[s["id"]] = "error"            # el monitor tumbó el pipeline: va sin él
                else:                                    # NDI y HDMI: los de la ruta
                    salidas[s["id"]] = "ok" if est == "en_marcha" else "esperando"
            res[rid] = {"estado": est, "errores": err, "salidas": salidas,
                        "lectores": info.get("lectores", 0),
                        "fps": st.get("fps_enviados", st.get("fps", 0)),
                        "audio_db": st.get("audio_db"),
                        "calibracion": getattr(procs.get("cam"), "calibracion", None),
                        "copia": any(c.startswith("prog:") for c, _ in por_ruta.get(rid, []))}
        return res


def trae_audio(info):
    """Si lo que alguien publica en MediaMTX lleva pista de audio (sus 'tracks':
    "H264", "MPEG-4 Audio", "Opus"...)."""
    return any(any(x in str(p).lower() for x in ("audio", "aac", "opus"))
               for p in info.get("pistas") or [])


def estado_proceso(p):
    """ok | esperando | error, con los estados de cada clase de proceso."""
    e = getattr(p, "estado", "")
    if e in ("emitiendo", "en_hdmi", "recibiendo", "activa"):
        return "ok"
    if e == "error":
        return "error"
    return "esperando"


class Fuentes:
    """Supervisa una Fuente por cámara: arranca, reinicia y para según las
    cámaras presentes y los ajustes."""

    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.fuentes = {}       # id -> Fuente
        self.audio = []
        self.hdmi_para = None   # id de la cámara que se ve por HDMI
        self.hdmi_conector = None  # conector DRM del monitor (hay que indicarlo para
                                   # cambiar su modo: kmssink coge el primero, aunque esté vacío)
        self.receptor = Receptor()  # o una fuente NDI de la red en el HDMI
        self.reemision = Auxiliar(REEMISION_PY, "reem")  # una fuente NDI grabada o emitida (Pro)
        self.programa = Auxiliar(PROGRAMA_PY, "programa")  # el programa final del puente (Pro)
        self.edicion = None     # lite | pro, según el equipo
        self.motor = MotorRutas(self)   # con motor "rutas", en vez de los modos
        self.reposos = {}           # conector -> Reposo: la pantalla de ayuda en cada monitor libre
        self.motor.soltar_hdmi = self.soltar_pantalla

    def objetivo(self, f, aj=None):
        aj = aj or self.config.fuente(f.cam["id"])
        modos = f.cam["modos"]
        ancho, alto = RESOLUCIONES.get(aj["resolucion"], RESOLUCIONES["1080p"])
        if not any(m[0] == ancho and m[1] == alto for m in modos):
            # la cámara no tiene ese modo: el mayor apaisado hasta 1080p
            cands = [m for m in modos if m[1] <= 1080 and m[0] >= m[1]] or modos
            ancho, alto = max(cands, key=lambda m: (m[0] * m[1], m[2]))[:2]
        mismos = [m for m in modos if m[0] == ancho and m[1] == alto]
        fps = max(m[2] for m in ([m for m in mismos if m[2] <= aj["fps"]] or mismos))
        fmt = self.formato(f, aj, [m[3] for m in mismos if m[2] == fps])
        latencia = aj.get("latencia", "normal")
        if latencia != "normal" and fps < 60:
            # a 60 fps cada espera de un fotograma dura la mitad; solo sin comprimir:
            # MJPEG a 1080p60 deja un Celeron al límite
            fmt60 = self.formato(f, aj, [m[3] for m in mismos if m[2] == 60])
            usb3 = velocidad_usb(f.cam.get("usb")) >= 5000     # por USB 2.0 no cabe
            if usb3 and fmt60 == "YUYV" and any(m[2] == 60 and m[3] == "YUYV" for m in mismos):
                fps, fmt = 60, fmt60
        audio = (None if latencia == "minima" or aj.get("solo_monitor")
                 else self.audio_para(f, aj["audio"]))
        return {"dev": f.cam["dev"], "ancho": ancho, "alto": alto, "fps": fps, "fmt": fmt,
                "nombre": aj["ndi_nombre"], "audio": audio,
                # audio de otra interfaz, que llega antes que la imagen: se retrasa
                "retraso_audio": int(aj.get("retraso_audio") or 0) if audio else 0,
                "baja_latencia": latencia == "baja" and audio is not None,
                "decodificadores": self.decodificadores(f, fmt),
                "hdmi": self.hdmi_conector if f.cam["id"] == self.hdmi_para
                and not f.hdmi_fallido else None,
                "gpu": None if aj.get("solo_monitor") else self.salida_gpu(aj, fmt),
                "gpu_extra": [] if aj.get("solo_monitor") else self.salidas_extra(aj, fmt),
                "solo_monitor": bool(aj.get("solo_monitor")),
                "preview": ruta_preview(f.cam["id"])}

    @staticmethod
    def formato(f, aj, disponibles):
        """MJPG o YUYV para el modo ya elegido. Sin comprimir se ahorra descomprimir
        el JPEG, que es lo que más CPU gasta, pero solo cabe por USB 3.0 (por USB 2.0,
        YUYV a 1080p no pasa de ~5 fps). La salida por GPU parte del JPEG: con ella
        activa, en automático se queda en MJPEG."""
        hay_mjpg, hay_crudo = "MJPG" in disponibles, "YUYV" in disponibles
        if not hay_mjpg:
            return "YUYV"
        if not hay_crudo:
            return "MJPG"
        quiere = aj.get("formato", "auto")
        if quiere == "mjpeg":
            return "MJPG"
        if quiere == "crudo":
            return "YUYV"
        usb3 = velocidad_usb(f.cam.get("usb")) >= 5000
        return "YUYV" if usb3 and aj.get("gpu_salida", "no") == "no" else "MJPG"

    def salidas_extra(self, aj, fmt):
        """Salidas H.264 además de la principal (grabar mientras se emite por SRT y
        por RTMP...): solo en un equipo potente. Cada una en su propio pipeline de
        la GPU, con los ajustes que la cámara ya tiene para ese tipo de salida."""
        principal = aj.get("gpu_salida", "no")
        if principal == "no" or not simultaneo(self.config.get()):
            return []
        extra = []
        for modo in SALIDAS_EXTRA:
            if modo != principal and modo in (aj.get("gpu_extra") or []):
                g = self.salida_gpu(aj, fmt, modo)
                if g:
                    extra.append(g)
        return extra

    @staticmethod
    def salida_gpu(aj, fmt, modo=None):
        """La GPU parte del JPEG de la cámara: solo con MJPEG."""
        modo = modo or aj.get("gpu_salida", "no")
        cap = capacidad_gpu()
        if modo == "no" or fmt != "MJPG" or not cap["disponible"]:
            return None
        g = {"modo": modo, "kbps": int(aj["gpu_kbps"]), "codificador": cap["codificador"],
             "cuando": "siempre", "vpp": capacidad_vpp()["vpp"]}
        if modo == "grabar":
            g["dir"] = GRABACIONES
            g["cuando"] = aj.get("grabar_cuando", "siempre")
            if g["cuando"] == "horario":
                g["horario"] = [aj["grabar_desde"], aj["grabar_hasta"]]
        elif modo == "srt":
            g["puerto"] = int(aj["srt_puerto"])
            g["latencia"] = int(aj.get("srt_latencia", 200))
        elif aj.get("rtmp_url"):
            g["url"] = aj["rtmp_url"]
        else:
            return None
        return g

    def decodificadores(self, f, fmt):
        if fmt != "MJPG":
            return 1
        ajuste = self.config.get()["decodificadores"]
        if ajuste != "auto":
            return int(ajuste)
        # en automático, la GPU si puede: gasta mucha menos CPU que cualquier reparto
        if capacidad_vpp()["jpeg"] and not f.deco_gpu_fallida:
            return "gpu"
        return f.hilos

    def audio_para(self, f, ajuste):
        if ajuste == "off":
            return None
        if ajuste == "auto":
            dev = next((a["dev"] for a in self.audio
                        if a["usb"] and a["usb"] == f.cam["usb"]), None)
        else:
            dev = next((a["dev"] for a in self.audio if a["id"] == ajuste), None)
        return None if dev == f.audio_fallido else dev

    def bucle(self):
        while True:
            try:
                self._paso()
            except Exception as ex:  # el supervisor no debe morir nunca
                evento("fallo_interno", detalle=str(ex))
            nucleo.latido("fuentes")     # para el watchdog de systemd
            time.sleep(2)

    def _paso(self):
        ed = edicion()
        if ed["edicion"] != self.edicion and not ed.get("midiendo"):
            # la primera vez, o al cambiar el equipo (más memoria, otra GPU, otro driver)
            self.edicion = ed["edicion"]
            evento("edicion", edicion=ed["edicion"], nucleos=ed["nucleos"],
                   memoria=ed["memoria_gb"], gpu=ed["gpu"], falta=ed["falta"])
        cams = {c["id"]: c for c in camaras()}
        self.audio = audio_dispositivos()
        cfg = self.config.get()
        if cfg.get("motor") == "rutas":
            return self._paso_rutas(cfg, cams)
        if self.motor.procs:
            # se vuelve a los modos: que suelten las cámaras y el HDMI antes de seguir
            esperar_fin(self.motor.detener())
        # una sola cámara al HDMI: la elegida o, en automático, la primera activa
        activas = [i for i in cams if self.config.fuente(i)["activa"]]
        monitor = next((p for p in pantallas() if p["conectado"] and p["id"]), None)
        self.hdmi_conector = monitor["id"] if monitor else None
        receptor = cfg["modo"] in ("receptor", "puente")
        puente = cfg["modo"] == "puente"
        # en un equipo potente las cámaras siguen emitiendo mientras recibe (sin el
        # HDMI, que es del receptor); en uno justo, se paran para dejarle la CPU
        pausar = receptor and not simultaneo(cfg)
        rx_obj = None
        pro = simultaneo(cfg)
        if puente and PUENTE.disponible():
            # en Pro, además el programa final y el reparto (HLS)
            PUENTE.arrancar(clave_puente(self.config), reparto=pro)
        elif not puente:
            PUENTE.detener()
        info_puente = PUENTE.info() if puente and PUENTE.disponible() else {}
        url_rx = self._programa(cfg, puente and pro, info_puente)
        # equipo potente: lo que llega al puente sale también como fuente NDI
        puente_ndi = puente and cfg["puente_ndi"] and pro
        # en Pro el HDMI del puente puede mostrar el programa, una cámara (el multiview
        # del ATEM por USB) o nada
        hdmi_puente = cfg["puente_hdmi"] if pro else "programa"
        monitor_rx = monitor if hdmi_puente == "programa" else None
        if url_rx and (monitor_rx or puente_ndi):
            # como un ATEM Streaming Bridge: lo que publique el ATEM, al HDMI
            rx_obj = {"origen": "puente", "nombre": "ATEM", "url": url_rx,
                      "decodificador": decodificador_h264(),
                      "hdmi": monitor_rx["id"] if monitor_rx else None,
                      "pantalla": tamano_pantalla(monitor_rx) if monitor_rx else None,
                      "gpu_h264": capacidad_vpp()["h264"] and not self.receptor.gpu_fallida}
            if puente_ndi:
                rx_obj["ndi_nombre"] = NOMBRE_NDI_PUENTE
                rx_obj["ndi_audio"] = any("audio" in str(p).lower() or "aac" in str(p).lower()
                                          for p in info_puente.get("pistas") or [])
            audio = audio_hdmi() if monitor_rx else None
            if self.receptor.destino != "puente":
                self.receptor.destino = "puente"
                self.receptor.fallos, self.receptor.audio_fallido = 0, None
                self.receptor.reintento = 0.0
            rx_obj["audio"] = None if audio == self.receptor.audio_fallido else audio
        elif receptor and not puente and monitor and cfg["rx_fuente"]:
            rx_obj = {"nombre": cfg["rx_fuente"], "hdmi": monitor["id"],
                      "equipo": f"NDIICAM {socket.gethostname()}",
                      "pantalla": tamano_pantalla(monitor),
                      "plano": not self.receptor.plano_fallido}
            audio = audio_hdmi()
            if rx_obj["nombre"] != self.receptor.destino:
                self.receptor.destino = rx_obj["nombre"]
                self.receptor.fallos, self.receptor.audio_fallido = 0, None
                self.receptor.reintento = 0.0
            rx_obj["audio"] = None if audio == self.receptor.audio_fallido else audio
        if puente and pro and hdmi_puente in activas and monitor:
            self.hdmi_para = hdmi_puente     # el multiview (u otra cámara) al monitor
        elif receptor or not cfg["hdmi"] or not monitor or not activas:
            self.hdmi_para = None
        else:
            self.hdmi_para = cfg["hdmi_fuente"] if cfg["hdmi_fuente"] in activas else activas[0]
        rx = self.receptor
        # el HDMI lo quiere el receptor, una cámara (encuadre) o, si no, la ayuda
        hdmi_ocupado = rx_obj is not None or (
            self.hdmi_para is not None and not self.fuentes[self.hdmi_para].hdmi_fallido
            if self.hdmi_para in self.fuentes else self.hdmi_para is not None)
        with self.lock:
            # la ayuda solo en el monitor de los modos, y solo si nadie lo usa
            self._reposos_en(cfg, cams, [] if hdmi_ocupado or not monitor else [monitor])
            # el HDMI es de uno solo: el receptor lo suelta antes de que una cámara lo coja
            if rx.proc is not None and rx.obj != rx_obj:
                rx.detener()
            if rx_obj is None:
                rx.estado, rx.error, rx.destino = "apagado", "", None
            for ident in list(self.fuentes):
                if ident not in cams:
                    self.fuentes[ident].detener()
                    evento("cam_desconectada", fuente=self.fuentes[ident].cam["nombre"])
                    del self.fuentes[ident]
            usados, puertos = set(), set()
            for ident, cam in cams.items():
                f = self.fuentes.get(ident)
                if f is None:
                    f = self.fuentes[ident] = Fuente(cam)
                    evento("cam_detectada", fuente=cam["nombre"])
                f.cam = cam
                aj = self.config.fuente(ident)
                # solo monitor (el multiview del ATEM): solo corre mientras el HDMI la muestra
                sin_hdmi = aj.get("solo_monitor") and (ident != self.hdmi_para or f.hdmi_fallido)
                if pausar or not aj["activa"] or sin_hdmi:
                    parada = ("pausada" if pausar else "desactivada" if not aj["activa"]
                              else "solo_monitor")
                    if f.proc is not None or f.estado != parada:
                        f.detener()
                        f.estado, f.error = parada, ""
                    continue
                obj = self.objetivo(f)
                # dos fuentes del mismo equipo no pueden llamarse igual
                base, n = obj["nombre"], 2
                while obj["nombre"] in usados:
                    obj["nombre"] = f"{base} {n}"
                    n += 1
                usados.add(obj["nombre"])
                for g in [obj["gpu"], *obj["gpu_extra"]]:
                    if g and g["modo"] == "srt":
                        # dos cámaras no pueden escuchar SRT en el mismo puerto
                        while g["puerto"] in puertos:
                            g["puerto"] += 1
                        puertos.add(g["puerto"])
                if (f.proc is not None and not f.forzar_reinicio and f.obj
                        and obj["retraso_audio"] != f.obj.get("retraso_audio")
                        and {**obj, "retraso_audio": 0} == {**f.obj, "retraso_audio": 0}):
                    # solo cambia el retraso del audio: se ajusta en marcha, sin cortar
                    f.orden({"retraso_audio": obj["retraso_audio"]})
                    f.obj = obj
                if f.proc is not None and (obj != f.obj or f.forzar_reinicio):
                    f.detener()
                    evento("aplicando", fuente=cam["nombre"])
                f.forzar_reinicio = False
                if f.proc is None and time.time() >= f.reintento:
                    f.arrancar(obj)
            # y lo coge después de que la cámara que lo tenía se reinicie sin él
            if rx_obj and rx.proc is None and time.time() >= rx.reintento:
                rx.arrancar(rx_obj)
            self._reemision(cfg, puertos)
            self._grabacion_condicional()

    def _paso_rutas(self, cfg, cams):
        """Con el motor de rutas: los modos se paran y manda lo que digan las rutas.
        Cada monitor conectado es una pantalla más: una ruta por pantalla, y en
        las que queden libres, la ayuda. Si la tarjeta no alquila pantallas, una."""
        monitores = [p for p in pantallas() if p["conectado"] and p["id"]]
        if len(monitores) > 1 and not (nucleo.PANTALLAS and nucleo.PANTALLAS.disponible()):
            monitores = monitores[:1]
        self.hdmi_conector = monitores[0]["id"] if monitores else None
        with self.lock:
            moribundos = []
            for f in self.fuentes.values():
                if f.proc is not None:
                    moribundos.append(f.detener())
            self.fuentes = {}
            for p in (self.receptor, self.programa, self.reemision):
                if p.proc is not None:
                    moribundos.append(p.detener())
            if moribundos:
                esperar_fin(moribundos)     # las cámaras y el HDMI, libres para las rutas
            self.motor.paso(cfg, cams, monitores)
            # en los monitores que ninguna ruta usa (o va a usar), la pantalla de
            # ayuda. Cuando una ruta coge uno, el motor para su ayuda antes de arrancar
            self._reposos_en(cfg, cams, [m for m in monitores if m["conector"] not in self.motor.pantallas_usadas])

    def _programa(self, cfg, activo, info_puente):
        """El programa final del puente (Pro): arranca o para programa.py y dice de
        dónde tienen que leer el HDMI y el NDI: del programa en cuanto está, y de lo
        que manda el ATEM tal cual mientras no (o si el programa falla)."""
        crudo = f"{PUERTO_RTMP_LOCAL}/live/{PUENTE.clave}" if info_puente.get("publicando") else None
        obj = None
        if activo and crudo:
            dev = next((a["dev"] for a in self.audio if a["id"] == cfg["programa_audio"]), None)
            obj = {"nombre": "programa", "entrada": crudo,
                   "salida": f"{PUERTO_RTMP_LOCAL}/{RUTA_PROGRAMA}", "audio": dev,
                   "retraso_audio": int(cfg["programa_retraso"]) if dev else 0,
                   "grabar": {"dir": GRABACIONES} if cfg["programa_grabar"] else None}
        pr = self.programa
        if (pr.proc is not None and obj and pr.obj
                and {**obj, "retraso_audio": 0} == {**pr.obj, "retraso_audio": 0}
                and obj["retraso_audio"] != pr.obj["retraso_audio"]):
            pr.orden({"retraso_audio": obj["retraso_audio"]})     # en marcha, sin cortar
            pr.obj = obj
        if pr.proc is not None and pr.obj != obj:
            pr.detener()
            pr.fallos, pr.reintento = 0, 0.0
        if obj is None:
            pr.estado, pr.error = "apagado", ""
        elif pr.proc is None and time.time() >= pr.reintento:
            pr.arrancar(obj)
        if obj is None:
            return crudo
        if PUENTE.ruta_lista(RUTA_PROGRAMA):
            return f"{PUERTO_RTMP_LOCAL}/{RUTA_PROGRAMA}"
        return crudo if pr.estado == "error" else None

    def objetivo_reemision(self, cfg, puertos=()):
        """Qué tiene que hacer la reemisión, o None: hace falta un equipo potente,
        una fuente elegida, alguna salida y la GPU para codificar."""
        cap = capacidad_gpu()
        if not (simultaneo(cfg) and cfg["reem_fuente"] and cap["disponible"]):
            return None
        salidas = []
        for modo in SALIDAS_EXTRA:
            if modo not in cfg["reem_salidas"]:
                continue
            if modo == "grabar":
                salidas.append({"modo": "grabar", "dir": GRABACIONES})
            elif modo == "srt":
                puerto = int(cfg["reem_srt_puerto"])
                while puerto in puertos:        # el de una cámara
                    puerto += 1
                salidas.append({"modo": "srt", "puerto": puerto,
                                "latencia": int(cfg["reem_srt_latencia"])})
            elif cfg["reem_rtmp_url"]:
                salidas.append({"modo": "rtmp", "url": cfg["reem_rtmp_url"]})
        if not salidas:
            return None
        return {"nombre": cfg["reem_fuente"], "equipo": f"NDIICAM {socket.gethostname()}",
                "codificador": cap["codificador"], "vpp": capacidad_vpp()["vpp"],
                "kbps": int(cfg["reem_kbps"]), "salidas": salidas}

    def _reemision(self, cfg, puertos):
        r = self.reemision
        obj = self.objetivo_reemision(cfg, puertos)
        if r.proc is not None and r.obj != obj:
            r.detener()
            r.fallos, r.reintento = 0, 0.0
        if obj is None:
            r.estado, r.error = "apagado", ""
        elif r.proc is None and time.time() >= r.reintento:
            r.arrancar(obj)

    def _grabacion_condicional(self):
        """Enciende o apaga la grabación GPU de cada cámara según su regla:
        solo con receptores NDI conectados, o dentro de un horario."""
        condicionales = []
        for f in self.fuentes.values():
            if f.proc is None or not f.obj:
                continue
            # solo grabar tiene condición, y hay una como mucho: principal o adicional
            g = next((g for g in [f.obj.get("gpu"), *(f.obj.get("gpu_extra") or [])]
                      if g and g.get("cuando", "siempre") != "siempre"), None)
            if g:
                condicionales.append((f, g))
        if not condicionales:
            return
        conexiones = None
        ahora = time.strftime("%H:%M")
        for f, g in condicionales:
            if g["cuando"] == "receptores":
                if conexiones is None:
                    conexiones = conexiones_por_pid()
                quiere = bool(conexiones.get(f.proc.pid))
            elif g["cuando"] == "manual":
                # el botón Grabar del panel; se guarda, así sigue grabando tras un reinicio
                quiere = bool(self.config.fuente(f.cam["id"]).get("grabar_manual"))
            else:
                quiere = en_horario(ahora, *g["horario"])
            if quiere != f.gpu_pedida:
                f.orden({"gpu_activa": quiere, "modo": g["modo"]})
                f.gpu_pedida = quiere

    def _reposo(self, nombre):
        return self.reposos.setdefault(nombre, Reposo())

    def soltar_pantalla(self, nombre):
        """La ayuda deja esa pantalla (una ruta la va a coger). Devuelve el proceso
        que muere, para esperarlo."""
        r = self.reposos.get(nombre)
        return r.detener() if r and r.proc is not None else None

    def _reposos_en(self, cfg, cams, monitores):
        """La pantalla de ayuda en cada monitor de la lista, y en ninguno más."""
        quiero = {m["conector"]: self.pantalla_reposo(cfg, cams, m) for m in monitores}
        for nombre, r in self.reposos.items():
            if r.proc is not None and r.obj != quiero.get(nombre):
                r.detener()
        for nombre, obj in quiero.items():
            r = self._reposo(nombre)
            if r.proc is None and time.time() >= r.reintento:
                r.arrancar(obj)

    def pantalla_reposo(self, cfg, cams, monitor):
        """Qué cuenta la pantalla de ayuda. Solo lo que cambia despacio: cada
        cambio relanza el proceso y parpadea el HDMI un instante."""
        modo = cfg["modo"]
        detalle = None
        if modo == "puente":
            estado = "esperando_atem"
            ip = ip_de(dispositivo_por_defecto() or "")
            detalle = f"rtmp://{ip or '…'}:{PUERTO_RTMP}/live" if PUENTE.disponible() else None
        elif modo == "receptor":
            if not cfg["rx_fuente"]:
                estado = "sin_fuente"
            else:
                estado = "sin_senal"
                detalle = cfg["rx_fuente"]
        else:
            estados = [f.estado for i, f in self.fuentes.items() if i in cams]
            if not cams:
                estado = "sin_camara"
            elif "emitiendo" in estados:
                estado = "emitiendo"
                host = socket.gethostname().upper()
                detalle = "  ·  ".join(f"{host} ({f.obj['nombre']})" for f in self.fuentes.values()
                                       if f.estado == "emitiendo" and f.obj)
            elif "iniciando" in estados:
                estado = "iniciando"
            else:
                estado = "desactivada"
        dev = dispositivo_por_defecto()
        ip = ip_de(dev) if dev and dev != AP_IF else None
        wifi = nucleo.RED.info_wifi()
        if wifi and dev == nucleo.RED.wifi:
            red = {"tipo": "wifi", "ssid": wifi.get("ssid", "")}
        elif ip:
            red = {"tipo": "cable"}
        else:
            red = None
        ap = {"ssid": cfg["ap_ssid"], "clave": cfg["ap_clave"]} if nucleo.RED.ap_activo else None
        url = f"http://{ip}/" if ip else (f"http://{AP_IP}/" if ap else "")
        return {"hdmi": monitor["id"], "pantalla": tamano_pantalla(monitor), "salida": monitor["conector"],
                "idioma": cfg["idioma"], "equipo": socket.gethostname(),
                "version": f"{VERSION} {'Pro' if self.edicion == 'pro' else 'Lite'}",
                "estado": estado, "modo": modo, "detalle": detalle, "ip": ip, "red": red,
                "url": url, "ap": ap}

    def calibrar(self, ident, segundos=8):
        """Mide el desfase entre el audio y la imagen de una cámara con unas palmadas."""
        with self.lock:
            f = self.fuentes.get(ident)
            if f is None or f.proc is None or not (f.obj or {}).get("audio"):
                return False
            f.calibracion = {"estado": "midiendo", "hasta": time.time() + segundos}
            f.orden({"calibrar": segundos})
            return True

    def reiniciar(self, ident):
        with self.lock:
            f = self.fuentes.get(ident)
            if f:
                f.forzar_reinicio, f.audio_fallido, f.reintento = True, None, 0.0
                f.hdmi_fallido = False
            # con rutas, la cámara vive en el motor
            self.motor.reiniciar(ident)
            return f is not None or any(isinstance(p, Fuente) and p.cam["id"] == ident
                                        for p in self.motor.procs.values())

    def reiniciar_todo(self):
        """Botón del panel por si algo se queda atascado: reinicia desde cero
        todas las cámaras y el receptor, olvidando los fallos anteriores."""
        with self.lock:
            for ident in list(self.fuentes):
                self.reiniciar(ident)
            rx = self.receptor
            rx.detener()
            rx.fallos, rx.audio_fallido, rx.reintento = 0, None, 0.0
            if rx.destino:
                rx.estado = "conectando"
            self.motor.reiniciar()

    def detener_todas(self):
        with self.lock:
            for f in self.fuentes.values():
                f.detener()
            self.receptor.detener()
            self.reemision.detener()
            self.programa.detener()
            self.motor.detener()
            for r in self.reposos.values():
                r.detener()

    def lista(self):
        with self.lock:
            return list(self.fuentes.values())

def velocidad_usb(ruta):
    """Mbit/s del enlace USB de una cámara (480 = USB 2.0, 5000 = USB 3.0), o 0."""
    if not ruta:
        return 0
    v = leer(os.path.join(ruta, "speed"), "0")
    try:
        return int(float(v))
    except ValueError:
        return 0


def en_horario(ahora, desde, hasta):
    """Si la hora HH:MM cae en el tramo [desde, hasta). Un tramo que pasa de
    medianoche (22:00 a 06:00) también vale."""
    if desde == hasta:
        return True
    if desde < hasta:
        return desde <= ahora < hasta
    return ahora >= desde or ahora < hasta


def ruta_preview(ident):
    """Dónde deja fuente.py la vista previa de una cámara (en /run, que es tmpfs)."""
    return f"/run/ndiicam/preview-{hashlib.sha1(ident.encode()).hexdigest()[:12]}.jpg"

def ocultar_clave_rtmp(url):
    """El último tramo de una URL RTMP es la clave de emisión: no se enseña."""
    if not url:
        return ""
    base, _, clave = url.rstrip("/").rpartition("/")
    return f"{base}/{'•' * 8}" if base and clave else "•" * 8
