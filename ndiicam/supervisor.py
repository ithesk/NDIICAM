# NDIICAM — procesos de cámara, receptor y pantalla de ayuda, y quién usa el HDMI.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
import hashlib
import json
import socket
import subprocess
import sys
import threading
import time

import nucleo
from nucleo import (AP_IF, AP_IP, FUENTE_PY, GRABACIONES, MAX_DECODIFICADORES, PUERTO_RTMP,
    RECEPTOR_PY, REPOSO_PY, RESOLUCIONES, VERSION, conexiones_por_pid, dispositivo_por_defecto,
    evento, ip_de, ips_propias)
from camaras import audio_dispositivos, audio_hdmi, camaras, pantallas, resolucion_pantalla
from gpu import capacidad_gpu, decodificador_h264, entorno_va
from puente import PUENTE, clave_puente


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
        self.retrasos = 0           # mediciones seguidas con la emisión por detrás de la cámara
        self.hdmi_fallido = False   # el HDMI hizo caer el pipeline: seguir sin él
        self.gpu = {"estado": None, "error": ""}   # salida H.264: activa | error
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
        self.gpu_pedida = None      # última orden de grabar (True/False) enviada al proceso
        self.proc = subprocess.Popen([sys.executable, FUENTE_PY, json.dumps(obj)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                     env=entorno_va())
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
            elif "error" in d:
                self._fallo(d["error"], d.get("elemento", ""))
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
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.obj = self.desde = None

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
        self.proc = subprocess.Popen([sys.executable, REPOSO_PY, json.dumps(obj)],
                                     stdout=subprocess.PIPE, text=True)
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
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.obj = None

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
        self.c = {}
        self.prev = None
        self.stats = {"fps": 0, "audio_activo": False, "audio_db": None, "latencia_ms": None}

    def arrancar(self, obj):
        self.obj = obj
        self.estado, self.error = "conectando", ""
        self.c, self.prev = {}, None
        self.stats = {"fps": 0, "audio_activo": False, "audio_db": None, "latencia_ms": None}
        self.proc = subprocess.Popen([sys.executable, RECEPTOR_PY, json.dumps(obj)],
                                     stdout=subprocess.PIPE, text=True, env=entorno_va())
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
        nombre = self.obj["nombre"]
        puente = self.obj.get("origen") == "puente"
        if error == "eos" or "demultiplex" in error.lower() or (
                puente and self.obj and (error in ("proceso", "arranque") or "rtmp" in error.lower()
                                         or "connect" in error.lower() or "stream" in error.lower())):
            error = "sin_senal"     # así avisa ndisrc de que la fuente dejó de emitir
        if self.obj.get("audio") and (elemento in ("asink", "qa") or "alsa" in error.lower()):
            # el audio HDMI hizo caer el pipeline: seguir sin él
            self.audio_fallido = self.obj["audio"]
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
        self.reintento = time.time() + espera

    def detener(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.obj = self.desde = None

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
        self.reposo = Reposo()      # y si no, la pantalla de ayuda

    def objetivo(self, f):
        aj = self.config.fuente(f.cam["id"])
        modos = f.cam["modos"]
        ancho, alto = RESOLUCIONES.get(aj["resolucion"], RESOLUCIONES["1080p"])
        if not any(m[0] == ancho and m[1] == alto for m in modos):
            # la cámara no tiene ese modo: el mayor apaisado hasta 1080p
            cands = [m for m in modos if m[1] <= 1080 and m[0] >= m[1]] or modos
            ancho, alto = max(cands, key=lambda m: (m[0] * m[1], m[2]))[:2]
        mismos = [m for m in modos if m[0] == ancho and m[1] == alto]
        fps = max(m[2] for m in ([m for m in mismos if m[2] <= aj["fps"]] or mismos))
        # MJPEG si existe: YUYV a 1080p no cabe por USB 2.0 a más de ~5 fps
        fmt = "MJPG" if any(m[2] == fps and m[3] == "MJPG" for m in mismos) else "YUYV"
        return {"dev": f.cam["dev"], "ancho": ancho, "alto": alto, "fps": fps, "fmt": fmt,
                "nombre": aj["ndi_nombre"], "audio": self.audio_para(f, aj["audio"]),
                "decodificadores": self.decodificadores(f, fmt),
                "hdmi": self.hdmi_conector if f.cam["id"] == self.hdmi_para
                and not f.hdmi_fallido else None,
                "gpu": self.salida_gpu(aj, fmt),
                "preview": ruta_preview(f.cam["id"]) if fmt == "MJPG" else None}

    @staticmethod
    def salida_gpu(aj, fmt):
        """La GPU parte del JPEG de la cámara: solo con MJPEG."""
        modo = aj.get("gpu_salida", "no")
        cap = capacidad_gpu()
        if modo == "no" or fmt != "MJPG" or not cap["disponible"]:
            return None
        g = {"modo": modo, "kbps": int(aj["gpu_kbps"]), "codificador": cap["codificador"],
             "cuando": "siempre"}
        if modo == "grabar":
            g["dir"] = GRABACIONES
            g["cuando"] = aj.get("grabar_cuando", "siempre")
            if g["cuando"] == "horario":
                g["horario"] = [aj["grabar_desde"], aj["grabar_hasta"]]
        elif modo == "srt":
            g["puerto"] = int(aj["srt_puerto"])
        elif aj.get("rtmp_url"):
            g["url"] = aj["rtmp_url"]
        else:
            return None
        return g

    def decodificadores(self, f, fmt):
        if fmt != "MJPG":
            return 1
        ajuste = self.config.get()["decodificadores"]
        return f.hilos if ajuste == "auto" else int(ajuste)

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
            time.sleep(2)

    def _paso(self):
        cams = {c["id"]: c for c in camaras()}
        self.audio = audio_dispositivos()
        # una sola cámara al HDMI: la elegida o, en automático, la primera activa
        cfg = self.config.get()
        activas = [i for i in cams if self.config.fuente(i)["activa"]]
        monitor = next((p for p in pantallas() if p["conectado"] and p["id"]), None)
        self.hdmi_conector = monitor["id"] if monitor else None
        receptor = cfg["modo"] in ("receptor", "puente")
        puente = cfg["modo"] == "puente"
        rx_obj = None
        if puente and PUENTE.disponible():
            PUENTE.arrancar(clave_puente(self.config))
        elif not puente:
            PUENTE.detener()
        if puente and monitor and PUENTE.disponible() and PUENTE.info().get("publicando"):
            # como un ATEM Streaming Bridge: lo que publique el ATEM, al HDMI
            rx_obj = {"origen": "puente", "nombre": "ATEM",
                      "url": f"rtmp://127.0.0.1:{PUERTO_RTMP}/live/{PUENTE.clave}",
                      "decodificador": decodificador_h264(), "hdmi": monitor["id"],
                      "pantalla": resolucion_pantalla(monitor)}
            audio = audio_hdmi()
            if self.receptor.destino != "puente":
                self.receptor.destino = "puente"
                self.receptor.fallos, self.receptor.audio_fallido = 0, None
                self.receptor.reintento = 0.0
            rx_obj["audio"] = None if audio == self.receptor.audio_fallido else audio
        elif receptor and not puente and monitor and cfg["rx_fuente"]:
            rx_obj = {"nombre": cfg["rx_fuente"], "hdmi": monitor["id"],
                      "equipo": f"NDIICAM {socket.gethostname()}",
                      "pantalla": resolucion_pantalla(monitor)}
            audio = audio_hdmi()
            if rx_obj["nombre"] != self.receptor.destino:
                self.receptor.destino = rx_obj["nombre"]
                self.receptor.fallos, self.receptor.audio_fallido = 0, None
                self.receptor.reintento = 0.0
            rx_obj["audio"] = None if audio == self.receptor.audio_fallido else audio
        if receptor or not cfg["hdmi"] or not monitor or not activas:
            self.hdmi_para = None
        else:
            self.hdmi_para = cfg["hdmi_fuente"] if cfg["hdmi_fuente"] in activas else activas[0]
        rx = self.receptor
        # el HDMI lo quiere el receptor, una cámara (encuadre) o, si no, la ayuda
        hdmi_ocupado = rx_obj is not None or (
            self.hdmi_para is not None and not self.fuentes[self.hdmi_para].hdmi_fallido
            if self.hdmi_para in self.fuentes else self.hdmi_para is not None)
        reposo_obj = None if hdmi_ocupado or not monitor else self.pantalla_reposo(cfg, cams, monitor)
        with self.lock:
            if self.reposo.proc is not None and self.reposo.obj != reposo_obj:
                self.reposo.detener()
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
                if receptor or not aj["activa"]:
                    parada = "pausada" if receptor else "desactivada"
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
                if obj["gpu"] and obj["gpu"]["modo"] == "srt":
                    # dos cámaras no pueden escuchar SRT en el mismo puerto
                    while obj["gpu"]["puerto"] in puertos:
                        obj["gpu"]["puerto"] += 1
                    puertos.add(obj["gpu"]["puerto"])
                if f.proc is not None and (obj != f.obj or f.forzar_reinicio):
                    f.detener()
                    evento("aplicando", fuente=cam["nombre"])
                f.forzar_reinicio = False
                if f.proc is None and time.time() >= f.reintento:
                    f.arrancar(obj)
            # y lo coge después de que la cámara que lo tenía se reinicie sin él
            if rx_obj and rx.proc is None and time.time() >= rx.reintento:
                rx.arrancar(rx_obj)
            if reposo_obj and self.reposo.proc is None and time.time() >= self.reposo.reintento:
                self.reposo.arrancar(reposo_obj)
            self._grabacion_condicional()

    def _grabacion_condicional(self):
        """Enciende o apaga la grabación GPU de cada cámara según su regla:
        solo con receptores NDI conectados, o dentro de un horario."""
        condicionales = [f for f in self.fuentes.values() if f.proc is not None and f.obj
                         and (f.obj.get("gpu") or {}).get("cuando", "siempre") != "siempre"]
        if not condicionales:
            return
        conexiones = None
        ahora = time.strftime("%H:%M")
        for f in condicionales:
            g = f.obj["gpu"]
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
                f.orden({"gpu_activa": quiere})
                f.gpu_pedida = quiere

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
        return {"hdmi": monitor["id"], "pantalla": resolucion_pantalla(monitor),
                "idioma": cfg["idioma"], "equipo": socket.gethostname(), "version": VERSION,
                "estado": estado, "modo": modo, "detalle": detalle, "ip": ip, "red": red,
                "url": url, "ap": ap}

    def reiniciar(self, ident):
        with self.lock:
            f = self.fuentes.get(ident)
            if f:
                f.forzar_reinicio, f.audio_fallido, f.reintento = True, None, 0.0
                f.hdmi_fallido = False
            return f is not None

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

    def detener_todas(self):
        with self.lock:
            for f in self.fuentes.values():
                f.detener()
            self.receptor.detener()
            self.reposo.detener()

    def lista(self):
        with self.lock:
            return list(self.fuentes.values())

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
