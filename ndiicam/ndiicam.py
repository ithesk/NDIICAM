#!/usr/bin/env python3
# NDIICAM — convierte un PC Intel con Linux en una cámara NDI.
# Copyright (C) 2026 Pablo Holguín
#
# Este programa es software libre: puedes redistribuirlo y/o modificarlo bajo
# los términos de la GNU General Public License publicada por la Free Software
# Foundation, versión 3 o (a tu elección) cualquier versión posterior.
# Se distribuye SIN NINGUNA GARANTÍA. Ver el fichero LICENSE.
"""Servicio de NDIICAM.

- Emite cada cámara UVC conectada (MJPEG o YUYV) como fuente NDI, con el audio
  de la propia cámara si lo tiene. Cada fuente corre en su proceso (fuente.py).
- Salida HDMI: una de las cámaras, para encuadrar.
- Modo receptor: en vez de emitir, muestra por HDMI una fuente NDI de la red
  con su audio (receptor.py). Las cámaras se paran: no hay CPU para las dos cosas.
- Sirve el panel web en el puerto 80: estado, ajustes, redes WiFi, IP fija,
  copias de seguridad y actualización.
- Si no hay ninguna red conocida, levanta una WiFi de configuración con portal
  cautivo (hostapd + dnsmasq). Si la tarjeta lo permite, en una interfaz
  virtual (ap0) sin soltar la WiFi cliente; si no, alternando con ella.

Los eventos y errores viajan como códigos: el panel los traduce.
"""
import base64
import datetime
import glob
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import yaml

VERSION = "0.2.0"
REPO = "ithesk/NDIICAM"
CONFIG = "/etc/ndiicam/config.json"
DATOS = "/var/lib/ndiicam"          # el servicio apunta NDI_CONFIG_DIR a DATOS/.ndi
AQUI = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(AQUI, "web", "index.html")
FUENTE_PY = os.path.join(AQUI, "fuente.py")
RECEPTOR_PY = os.path.join(AQUI, "receptor.py")
MODOS = ("emisor", "receptor")   # un Atom no da para emitir y recibir a la vez
RUN = "/run/ndiicam"
AP_IF = "ap0"
AP_IP = "10.42.0.1"
PERFIL_TEMPORAL = "ndiicam-nueva"
ESPERA_AP = 60          # segundos sin red antes de levantar la WiFi de configuración
CICLO_EXCLUSIVO = 180   # en modo exclusivo, cada cuánto se suelta el AP para buscar redes
PAUSA_EXCLUSIVO = 45    # y cuánto tiempo se deja a NetworkManager para conectar
RESOLUCIONES = {"720p": (1280, 720), "1080p": (1920, 1080)}
FPS_VALIDOS = (15, 24, 25, 30, 50, 60)
# valores para las cámaras sin ajustes propios; 'fuentes' guarda los de cada una
POR_DEFECTO = {"ndi_nombre": "NDIICAM", "resolucion": "1080p", "fps": 30, "audio": "auto",
               "fuentes": {}, "transporte": "tcp", "ap_ssid": "",
               "ap_clave": "ndiicam-setup", "pais": "", "clave_panel": "",
               "hdmi": False, "hdmi_fuente": "auto", "hdmi_consola": "nativa",
               "modo": "emisor", "rx_fuente": ""}
GRUB_NDIICAM = "/etc/default/grub.d/90-ndiicam.cfg"
# resolución de la pantalla al arrancar: la de la cámara para verla a pantalla completa
MODOS_CONSOLA = {"nativa": None, "1080p": "1920x1080@60", "720p": "1280x720@60"}
CLAVES_FUENTE = ("activa", "ndi_nombre", "resolucion", "fps", "audio")
NOMBRE_NDI = re.compile(r"^[\w .\-]{1,48}$")
NOMBRE_EQUIPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,30}[A-Za-z0-9])?$")


# ---------------------------------------------------------------- utilidades

def sh(cmd, timeout=15):
    """Ejecuta un comando y devuelve su salida ('' si falla)."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def leer(ruta, defecto=""):
    try:
        with open(ruta) as f:
            return f.read().strip()
    except OSError:
        return defecto


def escribir_privado(ruta, texto):
    """Escribe un fichero legible solo por root (puede llevar contraseñas)."""
    fd = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(texto)


def partir(linea):
    """Separa una línea de 'nmcli -t' respetando los ':' escapados."""
    campos, actual, escape = [], "", False
    for ch in linea:
        if escape:
            actual += ch
            escape = False
        elif ch == "\\":
            escape = True
        elif ch == ":":
            campos.append(actual)
            actual = ""
        else:
            actual += ch
    campos.append(actual)
    return campos


def nm_valores(texto):
    """Valores múltiples de 'nmcli -g' (separados por ' | ')."""
    return [v.strip() for v in texto.strip().split(" | ") if v.strip()]


def ip_de(interfaz):
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)",
                  sh(["ip", "-4", "-o", "addr", "show", "dev", interfaz]))
    return m.group(1) if m else None


def canal(freq):
    if not freq:
        return None
    return int((freq - 2407) / 5) if freq < 3000 else int((freq - 5000) / 5)


def interfaz_wifi():
    """Primera interfaz WiFi física (la que tiene phy80211), sin contar ap0."""
    for ruta in sorted(glob.glob("/sys/class/net/*/phy80211")):
        nombre = ruta.split("/")[4]
        if nombre != AP_IF and not nombre.startswith("p2p"):
            return nombre
    return None


def dispositivo_por_defecto():
    m = re.search(r"\bdev (\S+)", sh(["ip", "-4", "route", "show", "default"]))
    return m.group(1) if m else None


def ruta_usb(sysdev):
    """Dispositivo USB (sin la interfaz) de un nodo de /sys, o None."""
    p = os.path.realpath(sysdev)
    if "/usb" not in p:
        return None
    partes = p.split("/")
    while partes and ":" in partes[-1]:
        partes.pop()
    return "/".join(partes)


def resumen_clave(clave):
    sal = secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", clave.encode(), bytes.fromhex(sal), 120000)
    return f"pbkdf2${sal}${h.hex()}"


def clave_correcta(clave, guardada):
    try:
        _, sal, h = guardada.split("$")
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", clave.encode(), bytes.fromhex(sal), 120000)
    return hmac.compare_digest(calc.hex(), h)


def version_tupla(texto):
    return tuple(int(x) for x in re.findall(r"\d+", texto or ""))


EVENTOS = deque(maxlen=80)


def evento(codigo, **datos):
    """Registra un evento; el panel lo muestra traducido."""
    EVENTOS.appendleft({"t": time.time(), "c": codigo, "d": datos})
    print(codigo, json.dumps(datos, ensure_ascii=False) if datos else "", flush=True)


# ------------------------------------------------------------- configuración

class Config:
    def __init__(self):
        self.lock = threading.Lock()
        self.datos = dict(POR_DEFECTO)
        try:
            with open(CONFIG) as f:
                self.datos.update(json.load(f))
        except (OSError, ValueError):
            pass
        if not isinstance(self.datos.get("fuentes"), dict):
            self.datos["fuentes"] = {}

    def get(self):
        with self.lock:
            d = json.loads(json.dumps(self.datos))
        if not d["ap_ssid"]:
            # nombre por defecto del AP: único por equipo
            wifi = interfaz_wifi()
            mac = leer(f"/sys/class/net/{wifi}/address") if wifi else ""
            d["ap_ssid"] = "NDIICAM-" + (mac.replace(":", "")[-4:].upper() or "SETUP")
        if not d["pais"]:
            m = re.search(r"country (\w\w):", sh(["iw", "reg", "get"]))
            d["pais"] = m.group(1) if m and m.group(1) != "00" else "US"
        return d

    def fuente(self, ident):
        """Ajustes efectivos de una cámara: los suyos sobre los por defecto."""
        d = self.get()
        f = {"activa": True, "ndi_nombre": d["ndi_nombre"], "resolucion": d["resolucion"],
             "fps": d["fps"], "audio": d["audio"]}
        f.update({k: v for k, v in d["fuentes"].get(ident, {}).items() if k in CLAVES_FUENTE})
        return f

    def actualizar(self, cambios):
        with self.lock:
            self.datos.update(cambios)
            self._guardar()

    def actualizar_fuente(self, ident, cambios):
        with self.lock:
            self.datos["fuentes"].setdefault(ident, {}).update(cambios)
            self._guardar()

    def restablecer(self):
        with self.lock:
            self.datos = dict(POR_DEFECTO)
            self.datos["fuentes"] = {}
            try:
                os.remove(CONFIG)
            except OSError:
                pass

    def _guardar(self):
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        escribir_privado(CONFIG + ".tmp", json.dumps(self.datos, indent=2, ensure_ascii=False))
        os.replace(CONFIG + ".tmp", CONFIG)


def configurar_ndi(transporte):
    """Fichero de configuración del NDI SDK. Con TCP, los receptores que no
    consiguen el vídeo por UDP fiable (RUDP) sí lo reciben; UDP = valores del
    SDK. El SDK lo lee una sola vez al arrancar cada proceso."""
    ruta = os.path.join(DATOS, ".ndi", "ndi-config.v1.json")
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    if transporte == "tcp":
        with open(ruta, "w") as f:
            json.dump({"ndi": {
                "rudp": {"send": {"enable": False}, "recv": {"enable": False}},
                "multitcp": {"send": {"enable": True}, "recv": {"enable": True}},
                "tcp": {"send": {"enable": True}, "recv": {"enable": True}},
                "multicast": {"send": {"enable": False}, "recv": {"enable": False}},
            }}, f, indent=2)
    elif os.path.exists(ruta):
        os.remove(ruta)


def reiniciar_servicio(espera=1.5):
    """Sale para que systemd vuelva a arrancar el servicio (Restart=always)."""
    def salir():
        RED.detener_ap()
        FUENTES.detener_todas()
        os._exit(0)
    threading.Timer(espera, salir).start()


def cambiar_nombre_equipo(nuevo):
    sh(["hostnamectl", "set-hostname", nuevo])
    hosts = leer("/etc/hosts")
    if re.search(r"^127\.0\.1\.1\s", hosts, re.M):
        hosts = re.sub(r"^127\.0\.1\.1\s.*$", f"127.0.1.1\t{nuevo}", hosts, flags=re.M)
    else:
        hosts += f"\n127.0.1.1\t{nuevo}"
    with open("/etc/hosts", "w") as f:
        f.write(hosts.rstrip("\n") + "\n")
    sh(["systemctl", "restart", "avahi-daemon"])


# ------------------------------------------------------------------- cámaras

_cache_modos = {}


def modos_captura(dev):
    """Modos (ancho, alto, fps, formato) en MJPG y YUYV, cacheados por nodo."""
    try:
        st = os.stat(dev)
    except OSError:
        return []
    clave = (dev, st.st_ino, st.st_ctime)
    if clave not in _cache_modos:
        modos, fmt, tam = set(), None, None
        for linea in sh(["v4l2-ctl", "-d", dev, "--list-formats-ext"]).splitlines():
            m = re.search(r"\[\d+\]: '(\w+)'", linea)
            if m:
                fmt = m.group(1)
                continue
            m = re.search(r"Size: Discrete (\d+)x(\d+)", linea)
            if m:
                tam = (int(m.group(1)), int(m.group(2)))
                continue
            m = re.search(r"\(([\d.]+) fps\)", linea)
            if m and fmt in ("MJPG", "YUYV") and tam:
                modos.add((tam[0], tam[1], round(float(m.group(1))), fmt))
        _cache_modos[clave] = sorted(modos)
    return _cache_modos[clave]


def camaras():
    """Cámaras de captura con algún modo utilizable, ordenadas por id. El id
    es estable entre reinicios (el nombre en /dev/v4l/by-id)."""
    ids = {os.path.realpath(p): os.path.basename(p)
           for p in glob.glob("/dev/v4l/by-id/*")}
    res = []
    for sysdir in glob.glob("/sys/class/video4linux/video*"):
        if leer(f"{sysdir}/index", "0") != "0":
            continue
        dev = "/dev/" + os.path.basename(sysdir)
        modos = modos_captura(dev)
        if modos:
            res.append({"id": ids.get(dev, dev), "dev": dev,
                        "nombre": leer(f"{sysdir}/name", "Camera"), "modos": modos,
                        "usb": ruta_usb(f"{sysdir}/device")})
    return sorted(res, key=lambda c: c["id"])


def pantallas():
    """Salidas de vídeo de la GPU y si hay un monitor conectado."""
    res = []
    for ruta in sorted(glob.glob("/sys/class/drm/card*-*")):
        if not os.path.exists(f"{ruta}/status"):
            continue
        modos = leer(f"{ruta}/modes").split()
        res.append({"conector": os.path.basename(ruta).split("-", 1)[1],
                    "id": int(leer(f"{ruta}/connector_id", "0")) or None,
                    "conectado": leer(f"{ruta}/status") == "connected",
                    "modo": modos[0] if modos else None})
    return res


def resolucion_pantalla(monitor):
    """[ancho, alto] a los que está la pantalla: la pedida al arrancar (video=)
    o, si no, la preferida del monitor, que es la que pone el kernel."""
    m = re.search(r"\bvideo=(?:[\w-]+:)?(\d+)x(\d+)", leer("/proc/cmdline"))
    if not m:
        m = re.match(r"(\d+)x(\d+)", monitor.get("modo") or "")
    return [int(m.group(1)), int(m.group(2))] if m else None


def modo_consola_activo():
    """Resolución de pantalla pedida al kernel en este arranque (video=)."""
    m = re.search(r"\bvideo=(?:[\w-]+:)?(\d+x\d+)", leer("/proc/cmdline"))
    if not m:
        return "nativa"
    return {"1920x1080": "1080p", "1280x720": "720p"}.get(m.group(1), m.group(1))


def fijar_modo_consola(modo):
    """Escribe el parámetro video= del kernel en un fichero propio de GRUB."""
    if not os.path.isdir(os.path.dirname(GRUB_NDIICAM)) or not shutil.which("update-grub"):
        return False
    if MODOS_CONSOLA[modo]:
        with open(GRUB_NDIICAM, "w") as f:
            f.write("# NDIICAM: resolución de la pantalla al arrancar, para ver la salida HDMI\n"
                    "# a pantalla completa. Se cambia desde el panel; borrar este fichero y\n"
                    "# ejecutar update-grub la devuelve a la nativa.\n"
                    f'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT video={MODOS_CONSOLA[modo]}"\n')
    elif os.path.exists(GRUB_NDIICAM):
        os.remove(GRUB_NDIICAM)
    return subprocess.run(["update-grub"], capture_output=True, timeout=180).returncode == 0


def audio_dispositivos():
    """Tarjetas ALSA con captura. El id es el nombre corto de ALSA."""
    largos = {}
    for linea in leer("/proc/asound/cards").splitlines():
        m = re.match(r"\s*(\d+) \[(\S+)\s*\]: \S+ - (.*)", linea)
        if m:
            largos[m.group(2)] = m.group(3).strip()
    res = []
    for cardir in sorted(glob.glob("/sys/class/sound/card*")):
        n = os.path.basename(cardir)[4:]
        if not glob.glob(f"/proc/asound/card{n}/pcm*c"):
            continue
        ident = leer(f"{cardir}/id")
        if not ident:
            continue
        res.append({"id": ident, "nombre": largos.get(ident, ident),
                    "dev": f"plughw:CARD={ident},DEV=0", "usb": ruta_usb(f"{cardir}/device")})
    return res


def audio_hdmi():
    """Dispositivo ALSA de salida por HDMI, si la GPU lo tiene."""
    for info in sorted(glob.glob("/proc/asound/card*/pcm*p/info")):
        texto = leer(info)
        if "hdmi" not in texto.lower():
            continue
        n = info.split("/")[3][4:]
        ident = leer(f"/sys/class/sound/card{n}/id")
        if ident:
            return f"hdmi:CARD={ident},DEV=0"
    return None


def ips_propias():
    return {d.split()[3].split("/")[0] for d in
            sh(["ip", "-o", "addr", "show"]).splitlines() if len(d.split()) > 3}


# ------------------------------------------------------------------- fuentes

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
        self.hdmi_fallido = False   # el HDMI hizo caer el pipeline: seguir sin él
        self.c = {"cam": 0, "bytes": 0, "out": 0, "desc": 0, "aud": 0}
        self.prev = None
        self.stats = {"fps_camara": 0, "fps_enviados": 0, "mbps_camara": 0,
                      "descartes": 0, "audio_activo": False}

    def arrancar(self, obj):
        self.obj = obj
        self.estado, self.error, self.sin_flujo = "iniciando", "", None
        self.c = {}
        self.prev = None
        self.proc = subprocess.Popen([sys.executable, FUENTE_PY, json.dumps(obj)],
                                     stdout=subprocess.PIPE, text=True)
        threading.Thread(target=self._leer, args=(self.proc,), daemon=True).start()

    def _leer(self, proc):
        for linea in proc.stdout:
            try:
                d = json.loads(linea)
            except ValueError:
                continue
            if "error" in d:
                self._fallo(d["error"], d.get("elemento", ""))
            else:
                self.c = d
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
                          "audio_activo": c["aud"] > p["aud"]}
            if fps_out > 0.5:
                self.sin_flujo = None
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
        self.stats = {"fps": 0, "audio_activo": False}

    def arrancar(self, obj):
        self.obj = obj
        self.estado, self.error = "conectando", ""
        self.c, self.prev = {}, None
        self.stats = {"fps": 0, "audio_activo": False}
        self.proc = subprocess.Popen([sys.executable, RECEPTOR_PY, json.dumps(obj)],
                                     stdout=subprocess.PIPE, text=True)
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
        if error == "eos" or "demultiplex" in error.lower():
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
            espera = min(15, 3 * self.fallos)
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
                          "audio_activo": c["aud"] > p["aud"]}
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
                "hdmi": self.hdmi_conector if f.cam["id"] == self.hdmi_para
                and not f.hdmi_fallido else None}

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
        receptor = cfg["modo"] == "receptor"
        rx_obj = None
        if receptor and monitor and cfg["rx_fuente"]:
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
        with self.lock:
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
            usados = set()
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
                if f.proc is not None and (obj != f.obj or f.forzar_reinicio):
                    f.detener()
                    evento("aplicando", fuente=cam["nombre"])
                f.forzar_reinicio = False
                if f.proc is None and time.time() >= f.reintento:
                    f.arrancar(obj)
            # y lo coge después de que la cámara que lo tenía se reinicie sin él
            if rx_obj and rx.proc is None and time.time() >= rx.reintento:
                rx.arrancar(rx_obj)

    def reiniciar(self, ident):
        with self.lock:
            f = self.fuentes.get(ident)
            if f:
                f.forzar_reinicio, f.audio_fallido, f.reintento = True, None, 0.0
                f.hdmi_fallido = False
            return f is not None

    def detener_todas(self):
        with self.lock:
            for f in self.fuentes.values():
                f.detener()
            self.receptor.detener()

    def lista(self):
        with self.lock:
            return list(self.fuentes.values())


# ----------------------------------------------------------------------- red

class Red:
    def __init__(self, config):
        self.config = config
        self.wifi = interfaz_wifi()
        self.hostapd = self.dnsmasq = None
        self.ap_activo = False
        self.ap_if = None
        self.modo_ap = None         # simultaneo | exclusivo
        self.simultaneo_falla = False
        self.ap_desde = 0.0
        self.ultimo_online = time.time()
        self.online_desde = None
        self.forzado_hasta = 0.0
        self.reintento_ap = 0.0
        self.pausa_ap_hasta = 0.0
        self.conectando = False
        self.conexion = None
        self.cambio_ip = None
        self.escaneo = []
        self._limpiar_ap()

    # --- estado

    def dispositivos(self):
        devs = []
        for linea in sh(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION",
                         "dev"]).splitlines():
            p = partir(linea)
            if len(p) >= 4:
                devs.append({"dev": p[0], "tipo": p[1], "estado": p[2],
                             "conexion": p[3]})
        return devs

    def online(self):
        return any(d["estado"] == "connected" and d["tipo"] in ("wifi", "ethernet")
                   and d["dev"] != AP_IF for d in self.dispositivos())

    def info_wifi(self):
        if not self.wifi or (self.ap_activo and self.modo_ap == "exclusivo"):
            return None
        out = sh(["iw", "dev", self.wifi, "link"])
        if "Connected to" not in out:
            return None

        def g(rx, tipo=str):
            m = re.search(rx, out)
            return tipo(m.group(1).strip()) if m else None
        senal = g(r"signal: (-?\d+)", int)
        freq = g(r"freq: ([\d.]+)", float)
        return {"ssid": g(r"SSID: (.+)"), "senal_dbm": senal,
                "senal_pct": None if senal is None else max(0, min(100, 2 * (senal + 100))),
                "banda": "5" if freq and freq > 4000 else "2.4",
                "canal": canal(freq), "tx_mbit": g(r"tx bitrate: ([\d.]+)", float),
                "rx_mbit": g(r"rx bitrate: ([\d.]+)", float), "ip": ip_de(self.wifi)}

    def clientes_ap(self):
        if not self.ap_if:
            return 0
        return sh(["iw", "dev", self.ap_if, "station", "dump"]).count("Station ")

    # --- bucle: WiFi de configuración

    def bucle(self):
        while True:
            try:
                self._paso()
            except Exception as ex:
                evento("fallo_interno", detalle=str(ex))
            time.sleep(5)

    def _paso(self):
        if not self.wifi:
            return
        ahora = time.time()
        online = self.online()
        forzado = ahora < self.forzado_hasta
        if online:
            self.ultimo_online = ahora
            if self.online_desde is None:
                self.online_desde = ahora
        else:
            self.online_desde = None
        if self.ap_activo:
            if self.hostapd.poll() is not None or self.dnsmasq.poll() is not None:
                evento("ap_caida")
                self.detener_ap()
            elif self.modo_ap == "simultaneo":
                if online and not forzado:
                    en_linea = ahora - self.online_desde
                    # dar tiempo a quien está en el portal para ver el resultado
                    if (en_linea > 30 and self.clientes_ap() == 0) or en_linea > 300:
                        self.detener_ap()
                        evento("ap_off_red")
                return
            else:
                # exclusivo: con el AP puesto no se pueden buscar redes, así que
                # cada cierto tiempo, si nadie lo usa, se suelta para intentarlo
                if (not forzado and self.clientes_ap() == 0
                        and ahora - self.ap_desde > CICLO_EXCLUSIVO):
                    self.detener_ap()
                    self.pausa_ap_hasta = ahora + PAUSA_EXCLUSIVO
                return
        if ahora < self.pausa_ap_hasta or self.conectando:
            return
        if forzado:
            self.iniciar_ap(solo_simultaneo=online)
        elif not online and ahora - self.ultimo_online > ESPERA_AP:
            self.iniciar_ap()

    def forzar_ap(self, minutos):
        """Activa la WiFi de configuración a mano. Con conexión, solo si la
        tarjeta admite el modo simultáneo (si no, se cortaría la red)."""
        if minutos and self.online() and self.simultaneo_falla:
            return False
        self.forzado_hasta = time.time() + 60 * minutos if minutos else 0.0
        return True

    def iniciar_ap(self, solo_simultaneo=False):
        if not self.wifi or time.time() < self.reintento_ap:
            return
        os.makedirs(RUN, mode=0o755, exist_ok=True)
        cfg = self.config.get()
        if not self.simultaneo_falla:
            if self._levantar_ap(AP_IF, cfg, crear=True):
                self.modo_ap = "simultaneo"
                return self._ap_listo(cfg)
            self.simultaneo_falla = True
            self.detener_ap()
            evento("ap_sin_simultaneo")
        if solo_simultaneo:
            return
        # exclusivo: buscar redes ahora, que luego la tarjeta estará ocupada
        self.redes(buscar=True)
        sh(["nmcli", "dev", "set", self.wifi, "managed", "no"])
        time.sleep(1)
        if self._levantar_ap(self.wifi, cfg, crear=False):
            self.modo_ap = "exclusivo"
            return self._ap_listo(cfg)
        self.detener_ap()
        self.reintento_ap = time.time() + 60
        evento("ap_fallo")

    def _ap_listo(self, cfg):
        self.ap_activo, self.ap_desde = True, time.time()
        evento("ap_activa", ssid=cfg["ap_ssid"], modo=self.modo_ap)

    def _levantar_ap(self, iface, cfg, crear):
        if crear:
            # MAC propia para ap0: la de la WiFi con el bit de administración local
            b = leer(f"/sys/class/net/{self.wifi}/address").split(":")
            b[0] = "%02x" % (int(b[0], 16) | 0x02)
            if subprocess.run(["iw", "dev", self.wifi, "interface", "add", AP_IF,
                               "type", "__ap", "addr", ":".join(b)],
                              capture_output=True).returncode:
                return False
        # mismo canal que la WiFi cliente si está en 2,4 GHz; si no, el 6
        w = self.info_wifi()
        ch = w["canal"] if w and w["banda"] == "2.4" and w["canal"] else 6
        escribir_privado(f"{RUN}/hostapd.conf", "\n".join([
            f"interface={iface}", "driver=nl80211", f"ssid={cfg['ap_ssid']}",
            "hw_mode=g", f"channel={ch}", f"country_code={cfg['pais']}",
            "ieee80211n=1", "wpa=2", "wpa_key_mgmt=WPA-PSK", "rsn_pairwise=CCMP",
            f"wpa_passphrase={cfg['ap_clave']}", ""]))
        escribir_privado(f"{RUN}/dnsmasq.conf", "\n".join([
            f"interface={iface}", "bind-interfaces", f"listen-address={AP_IP}",
            "no-resolv", "no-hosts", "dhcp-authoritative",
            "dhcp-range=10.42.0.10,10.42.0.100,255.255.255.0,10m",
            f"dhcp-option=3,{AP_IP}", f"dhcp-option=6,{AP_IP}",
            # RFC 8910: los móviles abren el portal directamente
            f"dhcp-option=114,http://{AP_IP}/",
            # portal cautivo: todos los nombres resuelven al panel
            f"address=/#/{AP_IP}",
            f"dhcp-leasefile={RUN}/dnsmasq.leases", f"pid-file={RUN}/dnsmasq.pid",
            ""]))
        self.ap_if = iface
        self.hostapd = subprocess.Popen(["hostapd", f"{RUN}/hostapd.conf"],
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        time.sleep(3)
        if self.hostapd.poll() is not None:
            return False
        sh(["ip", "addr", "add", f"{AP_IP}/24", "dev", iface])
        self.dnsmasq = subprocess.Popen(["dnsmasq", "-k", "-C", f"{RUN}/dnsmasq.conf"],
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        time.sleep(1)
        return self.dnsmasq.poll() is None

    def detener_ap(self):
        for proc in (self.dnsmasq, self.hostapd):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if self.wifi and self.ap_if == self.wifi:
            # modo exclusivo: devolver la tarjeta a NetworkManager
            sh(["ip", "addr", "flush", "dev", self.wifi])
            sh(["nmcli", "dev", "set", self.wifi, "managed", "yes"])
        self.hostapd = self.dnsmasq = self.ap_if = self.modo_ap = None
        self.ap_activo = False
        self._limpiar_ap()

    def _limpiar_ap(self):
        sh(["pkill", "-f", f"{RUN}/hostapd.conf"])
        sh(["pkill", "-f", f"{RUN}/dnsmasq.conf"])
        if os.path.exists(f"/sys/class/net/{AP_IF}"):
            sh(["iw", "dev", AP_IF, "del"])

    # --- redes WiFi

    def redes(self, buscar):
        if not self.wifi or (self.ap_activo and self.modo_ap == "exclusivo"):
            return self.escaneo     # la tarjeta está haciendo de AP
        out = sh(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN,FREQ",
                  "dev", "wifi", "list", "ifname", self.wifi,
                  "--rescan", "yes" if buscar else "auto"], timeout=30)
        vistas = {}
        for linea in out.splitlines():
            p = partir(linea)
            if len(p) < 6 or not p[1]:
                continue
            senal = int(p[2] or 0)
            previa = vistas.get(p[1])
            if previa is None or senal > previa["senal"]:
                vistas[p[1]] = {"ssid": p[1], "senal": senal, "seguridad": p[3],
                                "canal": p[4], "banda": "5" if p[5].startswith("5") else "2.4",
                                "activa": p[0] == "*" or bool(previa and previa["activa"])}
            elif p[0] == "*":
                previa["activa"] = True
        self.escaneo = sorted(vistas.values(), key=lambda r: -r["senal"])
        return self.escaneo

    def guardadas(self, con_claves=False):
        res = []
        for linea in sh(["nmcli", "-t", "-f", "NAME,TYPE,AUTOCONNECT-PRIORITY,ACTIVE",
                         "con", "show"]).splitlines():
            p = partir(linea)
            if len(p) < 4 or p[1] != "802-11-wireless" or p[0] == PERFIL_TEMPORAL:
                continue
            ssid = partir(sh(["nmcli", "-g", "802-11-wireless.ssid", "con", "show",
                              p[0]]).strip())[0]
            r = {"id": p[0], "ssid": ssid or p[0], "prioridad": int(p[2] or 0),
                 "activa": p[3] == "yes"}
            if con_claves:
                r["clave"] = sh(["nmcli", "-s", "-g", "802-11-wireless-security.psk",
                                 "con", "show", p[0]]).strip()
                r["seguridad"] = sh(["nmcli", "-g", "802-11-wireless-security.key-mgmt",
                                     "con", "show", p[0]]).strip()
                r["ip"] = self.ip_perfil(p[0])
            res.append(r)
        return sorted(res, key=lambda r: -r["prioridad"])

    def olvidar(self, ident):
        if ident.startswith("netplan-"):
            # las redes escritas a mano en netplan comparten definición: se
            # quitan del YAML y no con nmcli, que se llevaría las demás
            ssid = partir(sh(["nmcli", "-g", "802-11-wireless.ssid", "con", "show",
                              ident]).strip())[0]
            for ruta in sorted(glob.glob("/etc/netplan/*.yaml")):
                with open(ruta) as f:
                    d = yaml.safe_load(f) or {}
                wifis = (d.get("network") or {}).get("wifis") or {}
                for iface, defn in list(wifis.items()):
                    aps = (defn or {}).get("access-points") or {}
                    if ssid not in aps:
                        continue
                    del aps[ssid]
                    if not aps:
                        del wifis[iface]
                    if not wifis:
                        d["network"].pop("wifis", None)
                    escribir_privado(ruta + ".tmp",
                                     yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
                    os.replace(ruta + ".tmp", ruta)
                    sh(["netplan", "generate"])
                    sh(["nmcli", "con", "reload"])
                    return True
        return subprocess.run(["nmcli", "con", "delete", ident],
                              capture_output=True).returncode == 0

    def olvidar_todas(self):
        for p in self.guardadas():
            self.olvidar(p["id"])

    def anadir_perfil(self, ssid, clave, seguridad=None, prioridad=40, ip=None,
                      nombre=None, autoconectar=True):
        args = ["nmcli", "con", "add", "type", "wifi", "ifname", self.wifi,
                "con-name", nombre or ssid, "ssid", ssid,
                "connection.autoconnect", "yes" if autoconectar else "no",
                "connection.autoconnect-retries", "0",
                "connection.autoconnect-priority", str(prioridad)]
        if clave:
            args += ["wifi-sec.key-mgmt", seguridad or "wpa-psk", "wifi-sec.psk", clave]
        if ip and ip.get("metodo") == "manual":
            args += self._args_ip(ip)
        return subprocess.run(args, capture_output=True, text=True, timeout=20)

    def conectar(self, ssid, clave):
        if self.conectando or not self.wifi:
            return False
        self.conectando = True
        self.conexion = {"ssid": ssid, "estado": "conectando"}
        threading.Thread(target=self._conectar, args=(ssid, clave), daemon=True).start()
        return True

    def _conectar(self, ssid, clave):
        try:
            self._intentar_conexion(ssid, clave)
        except Exception as ex:
            self._resultado("error", motivo="generico", detalle=str(ex))
        finally:
            self.conectando = False

    def _intentar_conexion(self, ssid, clave):
        if self.ap_activo and self.modo_ap == "exclusivo":
            # la tarjeta es el AP: soltarlo para poder conectar
            self.detener_ap()
            time.sleep(4)
        # se prueba con un perfil temporal: si la clave está mal, el perfil que
        # ya hubiera guardado para esa red sigue intacto
        sh(["nmcli", "con", "delete", PERFIL_TEMPORAL])
        seguridad = next((r["seguridad"] for r in self.escaneo if r["ssid"] == ssid), None)
        if seguridad and not clave:
            return self._resultado("error", motivo="necesita_clave")
        solo_wpa3 = seguridad and "WPA3" in seguridad and "WPA2" not in seguridad
        r = self.anadir_perfil(ssid, clave, "sae" if solo_wpa3 else "wpa-psk",
                               nombre=PERFIL_TEMPORAL)
        if r.returncode:
            return self._resultado("error", motivo="perfil", detalle=r.stderr.strip())
        r = subprocess.run(["nmcli", "--wait", "45", "con", "up", PERFIL_TEMPORAL],
                           capture_output=True, text=True, timeout=60)
        if r.returncode:
            sh(["nmcli", "con", "delete", PERFIL_TEMPORAL])
            if "Secrets were required" in r.stderr or "802-1x" in r.stderr:
                motivo = "clave"
            elif "No network with SSID" in r.stderr or "not found" in r.stderr:
                motivo = "no_encontrada"
            else:
                motivo = "generico"
            return self._resultado("error", motivo=motivo)
        for p in self.guardadas():
            if p["ssid"] == ssid and p["id"] != PERFIL_TEMPORAL:
                self.olvidar(p["id"])
        sh(["nmcli", "con", "modify", PERFIL_TEMPORAL, "connection.id", ssid])
        self._resultado("ok", ip=ip_de(self.wifi), equipo=socket.gethostname())

    def _resultado(self, estado, **datos):
        self.conexion = {"ssid": self.conexion["ssid"], "estado": estado, **datos}
        evento("wifi_ok" if estado == "ok" else "wifi_error",
               ssid=self.conexion["ssid"], **datos)

    # --- dirección IP

    def ip_perfil(self, perfil):
        out = sh(["nmcli", "-g", "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns",
                  "con", "show", perfil]).splitlines() + ["", "", "", ""]
        direccion = (out[1].split(",")[0].strip() if out[1] else "")
        return {"metodo": out[0].strip() or "auto",
                "direccion": direccion.split("/")[0] if direccion else "",
                "prefijo": int(direccion.split("/")[1]) if "/" in direccion else 24,
                "gateway": out[2].strip(), "dns": [d for d in re.split(r"[,\s]+", out[3]) if d]}

    def ip_config(self):
        """Ajuste de IP del perfil que da la ruta por defecto y valores en uso."""
        dev = dispositivo_por_defecto()
        if not dev or dev == AP_IF:
            return None
        perfil = sh(["nmcli", "-g", "GENERAL.CONNECTION", "dev", "show", dev]).strip()
        if not perfil:
            return None
        actual = sh(["nmcli", "-g", "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS", "dev", "show",
                     dev]).splitlines() + ["", "", ""]
        direccion = (nm_valores(actual[0]) or [""])[0]
        return {"perfil": perfil, "dispositivo": dev, **self.ip_perfil(perfil),
                "en_uso": {"direccion": direccion.split("/")[0],
                           "prefijo": int(direccion.split("/")[1]) if "/" in direccion else 24,
                           "gateway": actual[1].strip(), "dns": nm_valores(actual[2])},
                "cambio": self.cambio_ip}

    @staticmethod
    def _args_ip(ip):
        if ip["metodo"] == "manual":
            return ["ipv4.method", "manual", "ipv4.addresses",
                    f"{ip['direccion']}/{ip['prefijo']}", "ipv4.gateway", ip["gateway"],
                    "ipv4.dns", " ".join(ip["dns"])]
        return ["ipv4.method", "auto", "ipv4.addresses", "", "ipv4.gateway", "", "ipv4.dns", ""]

    def cambiar_ip(self, nuevo):
        if self.cambio_ip and self.cambio_ip["estado"] == "aplicando":
            return False
        cfg = self.ip_config()
        if not cfg:
            return False
        self.cambio_ip = {"estado": "aplicando"}
        threading.Thread(target=self._cambiar_ip, args=(cfg, nuevo), daemon=True).start()
        return True

    def _cambiar_ip(self, cfg, nuevo):
        perfil, dev = cfg["perfil"], cfg["dispositivo"]
        previo = {k: cfg[k] for k in ("metodo", "direccion", "prefijo", "gateway", "dns")}

        def aplicar(ip):
            sh(["nmcli", "con", "modify", perfil] + self._args_ip(ip))
            sh(["nmcli", "--wait", "30", "con", "up", perfil], timeout=40)

        def funciona():
            # con conexión y la puerta de enlace respondiendo, el cambio vale
            for _ in range(10):
                time.sleep(2)
                gw = (nm_valores(sh(["nmcli", "-g", "IP4.GATEWAY", "dev", "show", dev]))
                      or [None])[0]
                if gw and "1 received" in sh(["ping", "-c1", "-W2", gw]):
                    return True
            return False

        try:
            aplicar(nuevo)
            if funciona():
                self.cambio_ip = {"estado": "ok", "direccion": ip_de(dev),
                                  "equipo": socket.gethostname()}
                evento("ip_cambiada", perfil=perfil, metodo=nuevo["metodo"], direccion=ip_de(dev))
                return
            aplicar(previo)
            self.cambio_ip = {"estado": "error", "motivo": "sin_conexion"}
            evento("ip_revertida", perfil=perfil)
        except Exception as ex:
            aplicar(previo)
            self.cambio_ip = {"estado": "error", "motivo": "generico", "detalle": str(ex)}
            evento("ip_revertida", perfil=perfil)


# --------------------------------------------------------------- actualizar

class Actualizador:
    def __init__(self):
        self.info = {"actual": VERSION, "ultima": None, "disponible": False,
                     "comprobado": None, "error": None, "en_curso": False, "url": None}
        marca = os.path.join(DATOS, "actualizando")
        if os.path.exists(marca):
            anterior = leer(marca)
            os.remove(marca)
            if anterior and anterior != VERSION:
                evento("actualizado", de=anterior, a=VERSION)
            elif anterior:
                evento("actualizacion_sin_cambio", version=VERSION)

    def comprobar(self):
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/releases/latest",
            headers={"User-Agent": f"NDIICAM/{VERSION}", "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.load(r)
            ultima = d.get("tag_name", "")
            self.info.update(ultima=ultima, url=d.get("html_url"), error=None,
                             disponible=version_tupla(ultima) > version_tupla(VERSION),
                             notas=(d.get("body") or "")[:2000])
            if self.info["disponible"]:
                evento("actualizacion_disponible", version=ultima)
        except urllib.error.HTTPError as ex:
            self.info.update(error="sin_versiones" if ex.code == 404 else f"http_{ex.code}")
        except (urllib.error.URLError, OSError, ValueError) as ex:
            self.info.update(error="sin_internet", detalle=str(ex))
        self.info["comprobado"] = time.time()
        return self.info

    def instalar(self):
        if self.info["en_curso"] or not self.info["disponible"]:
            return False
        tag = self.info["ultima"]
        self.info["en_curso"] = True
        os.makedirs(DATOS, exist_ok=True)
        with open(os.path.join(DATOS, "actualizando"), "w") as f:
            f.write(VERSION)
        registro = os.path.join(DATOS, "actualizacion.log")
        orden = (f"set -e; T=$(mktemp -d); trap 'rm -rf \"$T\"' EXIT; "
                 f"curl -fsSL https://github.com/{REPO}/archive/refs/tags/{shlex.quote(tag)}.tar.gz"
                 f" | tar -xz -C \"$T\"; cd \"$T\"/*; "
                 f"./install.sh --accept-ndi-license --no-network")
        sh(["systemctl", "reset-failed", "ndiicam-actualizar.service"])
        r = subprocess.run(["systemd-run", "--unit=ndiicam-actualizar", "--collect",
                            "-p", f"StandardOutput=append:{registro}",
                            "-p", f"StandardError=append:{registro}",
                            "/bin/bash", "-c", orden], capture_output=True, text=True)
        if r.returncode:
            self.info["en_curso"] = False
            evento("actualizacion_error", detalle=r.stderr.strip())
            return False
        evento("actualizando", version=tag)
        return True

    def bucle(self):
        time.sleep(90)
        while True:
            try:
                self.comprobar()
            except Exception as ex:
                evento("fallo_interno", detalle=str(ex))
            time.sleep(24 * 3600)


# ------------------------------------------------------------------ respaldo

def respaldo():
    return {"ndiicam": VERSION, "fecha": datetime.datetime.now().isoformat(timespec="seconds"),
            "equipo": socket.gethostname(), "config": CONF.get(),
            "wifi": RED.guardadas(con_claves=True) if RED.wifi else []}


def restaurar(d):
    """Aplica una copia: ajustes, redes WiFi que falten y nombre del equipo."""
    cfg = d.get("config") or {}
    cambios = {k: cfg[k] for k in POR_DEFECTO if k in cfg}
    if not isinstance(cambios.get("fuentes", {}), dict):
        cambios.pop("fuentes", None)
    if cambios:
        CONF.actualizar(cambios)
    anadidas = 0
    if RED.wifi:
        actuales = {p["ssid"] for p in RED.guardadas()}
        for w in d.get("wifi") or []:
            ssid = str(w.get("ssid", ""))
            if not ssid or ssid in actuales:
                continue
            r = RED.anadir_perfil(ssid, str(w.get("clave", "")), w.get("seguridad") or None,
                                  int(w.get("prioridad", 0)), w.get("ip"))
            anadidas += r.returncode == 0
    equipo = str(d.get("equipo", ""))
    if equipo and NOMBRE_EQUIPO.match(equipo) and equipo != socket.gethostname():
        cambiar_nombre_equipo(equipo)
    evento("respaldo_restaurado", fecha=d.get("fecha", ""), wifi=anadidas)


def valores_fabrica():
    CONF.restablecer()
    configurar_ndi(POR_DEFECTO["transporte"])
    if RED.wifi:
        RED.olvidar_todas()
    evento("fabrica")


# ------------------------------------------------------------------ medición

ESTADO = {}
_nombres = {}


def nombre_de(ip):
    """Nombre mDNS de un receptor (cacheado: avahi tarda la primera vez)."""
    if ip not in _nombres:
        out = sh(["avahi-resolve", "-a", ip], timeout=3).split()
        _nombres[ip] = out[1].removesuffix(".local") if len(out) > 1 else None
    return _nombres[ip]


def conexiones_por_pid():
    """IPs de receptores con conexión TCP abierta, por proceso emisor."""
    res = {}
    for linea in sh(["ss", "-tnpH", "state", "established"]).splitlines():
        m = re.search(r"pid=(\d+)", linea)
        p = linea.split()
        if not m or len(p) < 4 or p[2].rsplit(":", 1)[-1] == "80":
            continue
        ip = p[3].rsplit(":", 1)[0].strip("[]").removeprefix("::ffff:")
        if not ip.startswith("127.") and ip != "::1":
            res.setdefault(int(m.group(1)), set()).add(ip)
    return res


def bytes_tx():
    total = 0
    for ruta in glob.glob("/sys/class/net/*/statistics/tx_bytes"):
        if ruta.split("/")[4] not in ("lo", AP_IF):
            total += int(leer(ruta, "0"))
    return total


def energia_rapl():
    """Energía acumulada (µJ) del paquete y de los núcleos según Intel RAPL, y
    el máximo del contador para detectar la vuelta. Mide el SoC (CPU + GPU), no
    el equipo entero."""
    res = {}
    for d in glob.glob("/sys/class/powercap/intel-rapl:*"):
        nombre = leer(f"{d}/name")
        clave = "paquete" if nombre.startswith("package") else nombre if nombre == "core" else None
        if clave and clave not in res:
            try:
                res[clave] = (int(leer(f"{d}/energy_uj")), int(leer(f"{d}/max_energy_range_uj", "0")))
            except ValueError:
                pass
    return res


def vatios(ahora, antes, dt):
    if not antes or dt <= 0:
        return None
    delta = ahora[0] - antes[0]
    if delta < 0:
        delta += antes[1]       # el contador dio la vuelta
    return round(delta / dt / 1e6, 2)


def tiempos_cpu():
    v = list(map(int, leer("/proc/stat").splitlines()[0].split()[1:9]))
    return sum(v), v[3] + v[4]


def medidor():
    prev = None
    while True:
        try:
            t, tx, cpu, rapl = time.time(), bytes_tx(), tiempos_cpu(), energia_rapl()
            for f in FUENTES.lista():
                f.medir(t)
            FUENTES.receptor.medir(t)
            if prev:
                dt = t - prev["t"]
                potencia = {k: vatios(v, prev["rapl"].get(k), dt) for k, v in rapl.items()}
                ESTADO.update(construir_estado(
                    (tx - prev["tx"]) * 8 / dt / 1e6,
                    100 * (1 - (cpu[1] - prev["cpu"][1]) / max(1, cpu[0] - prev["cpu"][0])),
                    potencia))
            prev = {"t": t, "tx": tx, "cpu": cpu, "rapl": rapl}
        except Exception as ex:
            evento("fallo_interno", detalle=str(ex))
        time.sleep(2)


def estado_receptor():
    rx = FUENTES.receptor
    if rx.destino is None:
        return None
    c = rx.c if rx.proc is not None else {}
    return {"fuente": rx.destino, "estado": rx.estado, "error": rx.error, "desde": rx.desde,
            "ancho": c.get("ancho"), "alto": c.get("alto"), "fps_fuente": c.get("fps"),
            "fps": rx.stats["fps"] if rx.estado == "recibiendo" else 0,
            "audio": bool(rx.obj and rx.obj.get("audio")),
            "audio_activo": rx.estado == "recibiendo" and rx.stats["audio_activo"],
            "audio_fallido": rx.audio_fallido is not None}


def construir_estado(tx_mbps, cpu_pct, potencia):
    host = socket.gethostname()
    cfg = CONF.get()
    conexiones = conexiones_por_pid()
    fuentes, total_rx = [], 0
    for f in FUENTES.lista():
        aj = CONF.fuente(f.cam["id"])
        obj = f.obj or {}
        pid = f.proc.pid if f.proc else None
        rx = sorted(conexiones.get(pid, set())) if f.estado == "emitiendo" else []
        total_rx += len(rx)
        audio_dev = next((a for a in FUENTES.audio if a["dev"] == obj.get("audio")), None)
        res_sel = (obj["ancho"], obj["alto"]) if obj else None
        en_marcha = f.estado in ("iniciando", "emitiendo")
        fuentes.append({
            "id": f.cam["id"], "camara": f.cam["nombre"], "dispositivo": f.cam["dev"],
            "estado": f.estado, "error": f.error, "desde": f.desde,
            "nombre_ndi": f"{host.upper()} ({obj['nombre']})" if obj else None,
            "modo": f"{obj['ancho']}×{obj['alto']} @{obj['fps']}" if obj else None,
            "formato": obj.get("fmt"),
            "fps_camara": f.stats["fps_camara"] if en_marcha else 0,
            "fps_enviados": f.stats["fps_enviados"] if en_marcha else 0,
            "mbps_camara": f.stats["mbps_camara"] if en_marcha else 0,
            "descartes": f.stats["descartes"] if en_marcha else 0,
            "audio": {"dispositivo": audio_dev["nombre"] if audio_dev else None,
                      "activo": bool(obj.get("audio")) and f.stats["audio_activo"],
                      "fallido": f.audio_fallido is not None,
                      "disponible": any(a["usb"] and a["usb"] == f.cam["usb"]
                                        for a in FUENTES.audio)},
            "receptores": [{"ip": ip, "nombre": nombre_de(ip)} for ip in rx],
            "hdmi": bool(obj.get("hdmi")), "hdmi_fallido": f.hdmi_fallido,
            "ajustes": aj,
            "admite": {k: any(m[0] == v[0] and m[1] == v[1] for m in f.cam["modos"])
                       for k, v in RESOLUCIONES.items()},
            "fps_disponibles": sorted({m[2] for m in f.cam["modos"]
                                       if res_sel and (m[0], m[1]) == res_sel
                                       and m[2] in FPS_VALIDOS}),
        })
    estados = [x["estado"] for x in fuentes]
    receptor = cfg["modo"] == "receptor"
    rx_estado = estado_receptor()
    monitor = any(p["conectado"] for p in pantallas())
    if receptor:
        # en modo receptor, el estado general es el de la recepción
        if not monitor:
            agregado = "sin_monitor"
        elif not cfg["rx_fuente"]:
            agregado = "sin_fuente"
        else:
            agregado = {"recibiendo": "recibiendo", "error": "rx_error"}.get(
                rx_estado["estado"] if rx_estado else "", "conectando")
    elif not fuentes:
        agregado = "sin_camara"
    elif "emitiendo" in estados:
        agregado = "emitiendo"
    elif "iniciando" in estados:
        agregado = "iniciando"
    elif "error" in estados:
        agregado = "error"
    else:
        agregado = "desactivada"
    devs = RED.dispositivos()
    wifi = RED.info_wifi()
    eth = next((d for d in devs if d["tipo"] == "ethernet"), None)
    eth_ok = bool(eth and eth["estado"] == "connected")
    eth_info = None
    if eth:
        vel = leer(f"/sys/class/net/{eth['dev']}/speed", "")
        eth_info = {"conectada": eth_ok, "ip": ip_de(eth["dev"]) if eth_ok else None,
                    "velocidad": int(vel) if vel.isdigit() and int(vel) > 0 else None}
    if eth_ok:
        modo, capacidad = "ethernet", 0.9 * (eth_info["velocidad"] or 100)
    elif wifi:
        # en WiFi, lo útil para TCP ronda la mitad de la velocidad del enlace
        modo, capacidad = "wifi", 0.5 * (wifi["tx_mbit"] or 0)
    else:
        modo, capacidad = "sin_red", None
    mem = dict(re.findall(r"^(\w+):\s+(\d+)", leer("/proc/meminfo"), re.M))
    temps = [int(leer(z, "0")) / 1000 for z in
             glob.glob("/sys/class/thermal/thermal_zone*/temp")]
    return {
        "version": VERSION,
        "equipo": {"nombre": host,
                   "uptime": float(leer("/proc/uptime", "0").split()[0]),
                   "cpu": round(cpu_pct), "nucleos": os.cpu_count(),
                   "potencia_w": potencia.get("paquete"), "potencia_nucleos_w": potencia.get("core"),
                   "temp": round(max(temps), 1) if temps else None,
                   "ram_pct": round(100 * (1 - int(mem.get("MemAvailable", 0))
                                           / max(1, int(mem.get("MemTotal", 1)))))},
        "fuentes": fuentes,
        "audio_dispositivos": [{"id": a["id"], "nombre": a["nombre"], "usb": bool(a["usb"])}
                               for a in FUENTES.audio],
        "modo": cfg["modo"],
        "receptor": {"fuente": cfg["rx_fuente"], "estado": rx_estado,
                     "red_ndi": BUSCADOR.lista() if receptor else []},
        "hdmi": {"activa": cfg["hdmi"], "fuente": cfg["hdmi_fuente"], "mostrando": FUENTES.hdmi_para,
                 "pantallas": pantallas(), "consola": cfg["hdmi_consola"],
                 "consola_activa": modo_consola_activo(),
                 "reinicio_pendiente": cfg["hdmi_consola"] != modo_consola_activo()},
        "emision": {"estado": agregado, "emitiendo": estados.count("emitiendo"),
                    "activas": sum(1 for x in fuentes if x["ajustes"]["activa"]),
                    "receptores": total_rx},
        "red": {"modo": modo, "wifi": wifi, "ethernet": eth_info,
                "tiene_wifi": bool(RED.wifi),
                "tx_mbps": round(tx_mbps, 1),
                "capacidad_mbps": round(capacidad) if capacidad else None,
                "ip": RED.ip_config(),
                "ap": {"activo": RED.ap_activo, "modo": RED.modo_ap,
                       "ssid": cfg["ap_ssid"], "clave": cfg["ap_clave"],
                       "clientes": RED.clientes_ap() if RED.ap_activo else 0,
                       "simultaneo": not RED.simultaneo_falla,
                       "forzado_hasta": RED.forzado_hasta
                       if RED.forzado_hasta > time.time() else None}},
        "actualizacion": ACTUALIZADOR.info,
    }


# --------------------------------------------------------------------- panel

def cliente_ip(handler):
    return handler.client_address[0].removeprefix("::ffff:")


class Panel(BaseHTTPRequestHandler):
    server_version = "NDIICAM"

    def log_message(self, *args):
        pass

    def _json(self, datos, codigo=200):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _error(self, codigo_error, http=400):
        self._json({"error": codigo_error}, http)

    def _cuerpo(self, maximo=16384):
        n = min(int(self.headers.get("Content-Length") or 0), maximo)
        try:
            datos = json.loads(self.rfile.read(n) or b"{}")
            return datos if isinstance(datos, dict) else {}
        except ValueError:
            return {}

    def _desde_ap(self):
        return cliente_ip(self).startswith("10.42.0.")

    def _autorizado(self):
        """Contraseña opcional del panel. Desde la WiFi de configuración no se
        pide: quien está ahí ya conoce su clave, y así sirve para recuperarla."""
        guardada = CONF.get()["clave_panel"]
        if not guardada or self._desde_ap():
            return True
        cab = self.headers.get("Authorization", "")
        if cab.startswith("Basic "):
            try:
                clave = base64.b64decode(cab[6:]).decode().partition(":")[2]
            except ValueError:
                return False
            return clave_correcta(clave, guardada)
        return False

    def _pedir_clave(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="NDIICAM"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirigir_portal(self):
        """Desde el AP, cualquier otro host (detección de portal de los móviles,
        webs) se manda al panel."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if self._desde_ap() and host not in (AP_IP, f"{socket.gethostname().lower()}.local"):
            self.send_response(302)
            self.send_header("Location", f"http://{AP_IP}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        return False

    def do_GET(self):
        if self._redirigir_portal():
            return
        if not self._autorizado():
            return self._pedir_clave()
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            try:
                with open(WEB, "rb") as f:
                    cuerpo = f.read()
            except OSError:
                return self.send_error(500)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
        elif url.path == "/api/estado":
            cfg = CONF.get()
            self._json({**ESTADO, "config": {
                "transporte": cfg["transporte"], "ap_ssid": cfg["ap_ssid"],
                "ap_clave": cfg["ap_clave"], "pais": cfg["pais"],
                "clave_panel": bool(cfg["clave_panel"])},
                "desde_ap": self._desde_ap(), "eventos": list(EVENTOS)[:40],
                "conexion_wifi": RED.conexion})
        elif url.path == "/api/wifi/redes":
            buscar = parse_qs(url.query).get("buscar") == ["1"]
            self._json({"redes": RED.redes(buscar), "guardadas": RED.guardadas()})
        elif url.path == "/api/actualizacion":
            self._json(ACTUALIZADOR.comprobar() if parse_qs(url.query).get("comprobar")
                       else ACTUALIZADOR.info)
        elif url.path == "/api/respaldo":
            cuerpo = json.dumps(respaldo(), indent=2, ensure_ascii=False).encode()
            nombre = f"ndiicam-{socket.gethostname()}-{datetime.date.today():%Y%m%d}.json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{nombre}"')
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
        else:
            self._error("no_encontrado", 404)

    def do_POST(self):
        if not self._autorizado():
            return self._pedir_clave()
        ruta = urlparse(self.path).path
        if ruta == "/api/respaldo":
            d = self._cuerpo(maximo=262144)
            if "config" not in d and "wifi" not in d:
                return self._error("respaldo_invalido")
            restaurar(d)
            reiniciar_servicio()
            return self._json({"ok": True, "reinicio": True})
        d = self._cuerpo()
        if ruta == "/api/config":
            return self._config(d)
        if ruta == "/api/fuente":
            return self._fuente(d)
        if ruta == "/api/fuente/reiniciar":
            return self._json({"ok": FUENTES.reiniciar(str(d.get("id", "")))})
        if ruta == "/api/wifi/conectar":
            ssid, clave = str(d.get("ssid", "")), str(d.get("clave", ""))
            if not 1 <= len(ssid.encode()) <= 32:
                return self._error("ssid_invalido")
            if clave and not 8 <= len(clave) <= 63:
                return self._error("clave_wifi_invalida")
            if not RED.conectar(ssid, clave):
                return self._error("conexion_en_curso", 409)
            return self._json({"ok": True})
        if ruta == "/api/wifi/olvidar":
            ok = RED.olvidar(str(d.get("id", "")))
            if ok:
                evento("red_olvidada", ssid=str(d.get("ssid") or d.get("id")))
            return self._json({"ok": ok}, 200 if ok else 400)
        if ruta == "/api/red/ip":
            return self._ip(d)
        if ruta == "/api/ap":
            activar = bool(d.get("activar"))
            if not RED.forzar_ap(10 if activar else 0):
                return self._error("ap_no_simultaneo", 409)
            evento("ap_manual_on" if activar else "ap_manual_off")
            return self._json({"ok": True})
        if ruta == "/api/actualizacion/instalar":
            if ACTUALIZADOR.info["en_curso"]:
                return self._error("actualizacion_en_curso", 409)
            if not ACTUALIZADOR.instalar():
                return self._error("sin_actualizacion")
            return self._json({"ok": True})
        if ruta == "/api/fabrica":
            if d.get("confirmar") != "FABRICA":
                return self._error("confirmacion")
            valores_fabrica()
            reiniciar_servicio()
            return self._json({"ok": True, "reinicio": True})
        if ruta == "/api/sistema/reiniciar":
            evento("reinicio_equipo")
            threading.Timer(1.5, sh, args=(["systemctl", "reboot"],)).start()
            return self._json({"ok": True})
        self._error("no_encontrado", 404)

    def _fuente(self, d):
        ident = str(d.get("id", ""))
        fuente = next((f for f in FUENTES.lista() if f.cam["id"] == ident), None)
        if fuente is None:
            return self._error("camara_invalida")
        cambios = {}
        if "activa" in d:
            cambios["activa"] = bool(d["activa"])
        if "ndi_nombre" in d:
            v = str(d["ndi_nombre"]).strip()
            if not NOMBRE_NDI.match(v):
                return self._error("nombre_ndi_invalido")
            cambios["ndi_nombre"] = v
        if "resolucion" in d:
            if d["resolucion"] not in RESOLUCIONES:
                return self._error("resolucion_invalida")
            cambios["resolucion"] = d["resolucion"]
        if "fps" in d:
            if d["fps"] not in FPS_VALIDOS:
                return self._error("fps_invalido")
            cambios["fps"] = d["fps"]
        if "audio" in d:
            v = str(d["audio"])
            if v not in ("auto", "off") and v not in {a["id"] for a in FUENTES.audio}:
                return self._error("audio_invalido")
            cambios["audio"] = v
        antes = CONF.fuente(ident)
        cambios = {k: v for k, v in cambios.items() if antes.get(k) != v}
        if cambios:
            CONF.actualizar_fuente(ident, cambios)
            FUENTES.reiniciar(ident)
            evento("ajustes_fuente", fuente=fuente.cam["nombre"], campos=sorted(cambios))
        self._json({"ok": True})

    def _config(self, d):
        antes = CONF.get()
        cambios, reiniciar = {}, False
        if "hdmi" in d:
            cambios["hdmi"] = bool(d["hdmi"])
        if "hdmi_fuente" in d:
            v = str(d["hdmi_fuente"])
            if v != "auto" and v not in {f.cam["id"] for f in FUENTES.lista()}:
                return self._error("camara_invalida")
            cambios["hdmi_fuente"] = v
        if "modo" in d:
            if d["modo"] not in MODOS:
                return self._error("modo_invalido")
            cambios["modo"] = d["modo"]
        if "rx_fuente" in d:
            # una fuente de la red: vale aunque ahora no se vea (se reintenta)
            v = str(d["rx_fuente"])
            if len(v) > 200 or not v.isprintable():
                return self._error("fuente_ndi_invalida")
            cambios["rx_fuente"] = v
        if "hdmi_consola" in d:
            if d["hdmi_consola"] not in MODOS_CONSOLA:
                return self._error("modo_invalido")
            if d["hdmi_consola"] != antes["hdmi_consola"]:
                if not fijar_modo_consola(d["hdmi_consola"]):
                    return self._error("sin_grub")
                cambios["hdmi_consola"] = d["hdmi_consola"]
        if "transporte" in d:
            if d["transporte"] not in ("tcp", "udp"):
                return self._error("transporte_invalido")
            cambios["transporte"] = d["transporte"]
        if "ap_ssid" in d:
            v = str(d["ap_ssid"]).strip()
            if not 1 <= len(v.encode()) <= 32 or not v.isprintable():
                return self._error("ssid_invalido")
            cambios["ap_ssid"] = v
        if "ap_clave" in d:
            v = str(d["ap_clave"])
            if not 8 <= len(v) <= 63 or not v.isprintable():
                return self._error("clave_wifi_invalida")
            cambios["ap_clave"] = v
        if "pais" in d:
            v = str(d["pais"]).upper()
            if not re.fullmatch(r"[A-Z]{2}", v):
                return self._error("pais_invalido")
            cambios["pais"] = v
        if "clave_panel" in d:
            v = str(d["clave_panel"])
            if v and len(v) < 4:
                return self._error("clave_panel_corta")
            cambios["clave_panel"] = resumen_clave(v) if v else ""
        nuevo_nombre = None
        if "nombre_equipo" in d:
            v = str(d["nombre_equipo"]).strip()
            if not NOMBRE_EQUIPO.match(v):
                return self._error("nombre_equipo_invalido")
            if v != socket.gethostname():
                nuevo_nombre = v
        cambios = {k: v for k, v in cambios.items() if antes.get(k) != v}
        if cambios:
            CONF.actualizar(cambios)
            if "modo" in cambios:
                evento("modo", modo=cambios["modo"])
            if cambios.keys() - {"modo"}:
                evento("ajustes", campos=sorted(cambios.keys() - {"modo"}))
        if {"hdmi", "hdmi_fuente"} & cambios.keys():
            for f in FUENTES.lista():
                f.hdmi_fallido = False      # volver a intentarlo con el ajuste nuevo
        if "transporte" in cambios:
            configurar_ndi(cambios["transporte"])
            reiniciar = True    # el SDK solo lee su configuración al arrancar
        if nuevo_nombre:
            cambiar_nombre_equipo(nuevo_nombre)
            evento("nombre_equipo", nombre=nuevo_nombre)
            reiniciar = True    # el prefijo del nombre NDI sale del hostname
        if {"ap_ssid", "ap_clave", "pais"} & cambios.keys() and RED.ap_activo:
            RED.detener_ap()    # el bucle de red la levanta con los datos nuevos
        if reiniciar:
            reiniciar_servicio()
        self._json({"ok": True, "reinicio": reiniciar})

    def _ip(self, d):
        metodo = d.get("metodo")
        if metodo not in ("auto", "manual"):
            return self._error("ip_invalida")
        nuevo = {"metodo": metodo, "direccion": "", "prefijo": 24, "gateway": "", "dns": []}
        if metodo == "manual":
            try:
                red = ipaddress.ip_interface(f"{d.get('direccion')}/{int(d.get('prefijo', 24))}")
                gw = ipaddress.ip_address(str(d.get("gateway", "")))
                dns = [str(ipaddress.ip_address(x.strip()))
                       for x in re.split(r"[,\s]+", str(d.get("dns", ""))) if x.strip()]
            except ValueError:
                return self._error("ip_invalida")
            if red.network.prefixlen > 30 or gw not in red.network or gw == red.ip:
                return self._error("gateway_invalido")
            nuevo.update(direccion=str(red.ip), prefijo=red.network.prefixlen,
                         gateway=str(gw), dns=dns or [str(gw)])
        if not RED.cambiar_ip(nuevo):
            return self._error("cambio_ip_en_curso", 409)
        self._json({"ok": True})


class Servidor(ThreadingHTTPServer):
    """IPv4 e IPv6 en el mismo socket (<equipo>.local resuelve a ambos)."""
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def main():
    global CONF, FUENTES, RED, ACTUALIZADOR, BUSCADOR
    os.makedirs(DATOS, exist_ok=True)
    CONF = Config()
    configurar_ndi(CONF.get()["transporte"])
    FUENTES = Fuentes(CONF)
    BUSCADOR = BuscadorNDI()
    RED = Red(CONF)
    ACTUALIZADOR = Actualizador()
    evento("inicio", version=VERSION)
    for tarea in (FUENTES.bucle, RED.bucle, medidor, ACTUALIZADOR.bucle):
        threading.Thread(target=tarea, daemon=True).start()
    servidor = Servidor(("::", 80), Panel)

    def salir(*_):
        RED.detener_ap()
        FUENTES.detener_todas()
        os._exit(0)
    signal.signal(signal.SIGTERM, salir)
    signal.signal(signal.SIGINT, salir)
    servidor.serve_forever()


if __name__ == "__main__":
    main()
