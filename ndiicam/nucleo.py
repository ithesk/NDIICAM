# NDIICAM — constantes, utilidades, eventos y configuración; y los singletons que main() crea.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import threading
import time
from collections import deque


# Singletons del servicio: los crea main() en ndiicam.py; los demás módulos los
# usan como nucleo.X, nunca los importan por nombre (se crean después de importar)
CONF = None            # Config
RED = None             # Red
FUENTES = None         # Fuentes
ACTUALIZADOR = None    # Actualizador
BUSCADOR = None        # BuscadorNDI
CORTAFUEGOS = None     # Cortafuegos
SESIONES = None        # Sesiones del panel
INTENTOS = None        # LimiteIntentos de la contraseña
HTTPS = None           # contexto TLS del panel (None: solo HTTP, sin openssl)


VERSION = "0.5.0"
REPO = "ithesk/NDIICAM"
CONFIG = "/etc/ndiicam/config.json"
DATOS = "/var/lib/ndiicam"          # el servicio apunta NDI_CONFIG_DIR a DATOS/.ndi
AQUI = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(AQUI, "web", "index.html")
FUENTE_PY = os.path.join(AQUI, "fuente.py")
RECEPTOR_PY = os.path.join(AQUI, "receptor.py")
REEMISION_PY = os.path.join(AQUI, "reemision.py")
REPOSO_PY = os.path.join(AQUI, "reposo.py")     # pantalla de ayuda cuando el HDMI está libre
IDIOMAS = ("es", "en")
MODOS = ("emisor", "receptor", "puente")   # un Atom no da para emitir y recibir a la vez

# modo puente (como un ATEM Streaming Bridge): un ATEM, OBS o vMix publica por RTMP o
# SRT en MediaMTX y el receptor lo saca por HDMI. MediaMTX lo instala install.sh
MEDIAMTX = "/usr/local/lib/ndiicam/mediamtx"
PUERTO_RTMP = 1935
PUERTO_SRT = 8890
API_MEDIAMTX = "http://127.0.0.1:9997"
CLAVE_PUENTE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
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
               "modo": "emisor", "rx_fuente": "", "decodificadores": "auto",
               "puente_clave": "", "idioma": "es", "cortafuegos": True, "puertos_extra": "",
               "simultaneo": True, "puente_ndi": True,
               # reemisión (equipo potente): una fuente NDI de la red, grabada o emitida
               "reem_fuente": "", "reem_salidas": [], "reem_kbps": 8000,
               "reem_srt_puerto": 9100, "reem_srt_latencia": 200, "reem_rtmp_url": ""}
AP_CLAVE_DEFECTO = POR_DEFECTO["ap_clave"]

# decodificadores JPEG por fuente: jpegdec va en un solo hilo y con imágenes muy
# detalladas no llega a 30 fps a 1080p. En "auto" se arranca con uno y se reparte
# en más cuando la emisión se queda por detrás de la cámara
MAX_DECODIFICADORES = max(1, min(3, (os.cpu_count() or 2) - 1))
GRUB_NDIICAM = "/etc/default/grub.d/90-ndiicam.cfg"

# resolución de la pantalla al arrancar: la de la cámara para verla a pantalla completa
MODOS_CONSOLA = {"nativa": None, "1080p": "1920x1080@60", "720p": "1280x720@60"}
CLAVES_FUENTE = ("activa", "ndi_nombre", "resolucion", "fps", "audio",
                 "gpu_salida", "gpu_extra", "gpu_kbps", "srt_puerto", "rtmp_url",
                 "grabar_cuando", "grabar_desde", "grabar_hasta", "grabar_manual", "formato",
                 "latencia", "srt_latencia", "retraso_audio")
# latencia de la emisión NDI: normal (audio sincronizado), baja (la imagen sale sin
# esperar al audio de su intervalo; plugin parcheado) o minima (sin audio). En baja y
# minima se sube a 60 fps si la cámara los da sin comprimir. Medido en un Celeron
# J3355 con una Cam Link 4K a 30 fps: normal ~100 ms, sin audio ~33 ms
LATENCIAS = ("normal", "baja", "minima")
# formato que se pide a la cámara: auto (sin comprimir si el puerto es USB 3.0 y
# no hace falta el JPEG), mjpeg o crudo (YUYV). Medido con una Cam Link 4K en un
# Celeron J3355, a 1080p30: MJPEG 50 % de un núcleo, sin comprimir 13 %
FORMATOS = ("auto", "mjpeg", "crudo")
# cuándo graba la salida GPU: siempre, solo mientras haya receptores NDI, en un
# horario, o a mano con el botón Grabar del panel (grabar_manual)
GRABAR_CUANDO = ("siempre", "receptores", "horario", "manual")
HORA = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# salida H.264 por GPU de cada cámara (además del NDI): grabar a disco, SRT o RTMP
SALIDAS_GPU = ("no", "grabar", "srt", "rtmp")
SALIDAS_EXTRA = ("grabar", "srt", "rtmp")     # las que se pueden sumar a la principal
RETRASO_MAX_MS = 1000       # retraso del audio respecto a la imagen que se puede aplicar
NOMBRE_NDI_PUENTE = "Puente"    # la fuente NDI del modo puente: "EQUIPO (Puente)"
GRABACIONES = "/var/lib/ndiicam/grabaciones"
NOMBRE_GRABACION = re.compile(r"^[\w.-]{1,120}\.mp4$")
URL_RTMP = re.compile(r"^rtmps?://[^\s\"'<>]{3,500}$")
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

def cacheado(segundos):
    """Guarda el resultado de un método durante unos segundos (por argumentos).
    Para consultas a nmcli/iw que se piden cada 2 s y cambian cada muchos minutos:
    cada una es un proceso nuevo y despierta a NetworkManager y D-Bus.
    olvidar_cache(objeto) las borra todas, tras cambiar la red."""
    def deco(fn):
        def envoltura(self, *args, **kwargs):
            caches = self.__dict__.setdefault("_caches", {})
            clave = (fn.__name__, args, tuple(sorted(kwargs.items())))
            ahora = time.monotonic()
            hit = caches.get(clave)
            if hit and ahora - hit[0] < segundos:
                return hit[1]
            valor = fn(self, *args, **kwargs)
            caches[clave] = (ahora, valor)
            return valor
        envoltura.__name__ = fn.__name__
        envoltura.__doc__ = fn.__doc__
        return envoltura
    return deco


def olvidar_cache(objeto):
    objeto.__dict__.get("_caches", {}).clear()


def cache_funcion(segundos):
    """Como cacheado, para funciones sueltas (por argumentos)."""
    def deco(fn):
        guardado = {}

        def envoltura(*args):
            ahora = time.monotonic()
            hit = guardado.get(args)
            if hit and ahora - hit[0] < segundos:
                return hit[1]
            valor = fn(*args)
            guardado[args] = (ahora, valor)
            return valor
        envoltura.__name__ = fn.__name__
        envoltura.__doc__ = fn.__doc__
        envoltura.olvidar = guardado.clear
        return envoltura
    return deco


_PAIS = []


def pais_regulatorio():
    """País del dominio regulatorio WiFi (US si no hay): una vez por arranque.
    Antes se preguntaba a 'iw reg get' en cada lectura de la configuración."""
    if not _PAIS:
        m = re.search(r"country (\w\w):", sh(["iw", "reg", "get"]))
        _PAIS.append(m.group(1) if m and m.group(1) != "00" else "US")
    return _PAIS[0]


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

@cache_funcion(1.5)    # se pide desde varios hilos en cada ciclo de 2 s
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

@cache_funcion(1.5)    # se pide desde varios hilos en cada ciclo de 2 s
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
# los eventos se guardan en disco para ver, tras un reinicio, qué pasó antes
EVENTOS_ARCHIVO = os.path.join(DATOS, "eventos.jsonl")
EVENTOS_MAX_BYTES = 256 * 1024
_lock_eventos = threading.Lock()

def evento(codigo, **datos):
    """Registra un evento; el panel lo muestra traducido."""
    e = {"t": time.time(), "c": codigo, "d": datos}
    EVENTOS.appendleft(e)
    print(codigo, json.dumps(datos, ensure_ascii=False) if datos else "", flush=True)
    with _lock_eventos:
        try:
            with open(EVENTOS_ARCHIVO, "a") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
                grande = f.tell() > EVENTOS_MAX_BYTES
            if grande:      # recortar a la mitad más reciente
                with open(EVENTOS_ARCHIVO) as f:
                    lineas = f.readlines()
                with open(EVENTOS_ARCHIVO + ".tmp", "w") as f:
                    f.writelines(lineas[len(lineas) // 2:])
                os.replace(EVENTOS_ARCHIVO + ".tmp", EVENTOS_ARCHIVO)
        except OSError:
            pass

def cargar_eventos():
    """Los últimos eventos guardados, al arrancar. Tras un cuelgue el final del
    archivo puede quedar con ceros: esas líneas se ignoran."""
    try:
        with open(EVENTOS_ARCHIVO, errors="replace") as f:
            lineas = f.readlines()[-EVENTOS.maxlen:]
    except OSError:
        return
    for linea in lineas:
        try:
            e = json.loads(linea.strip("\x00\n "))
        except ValueError:
            continue
        if isinstance(e, dict) and {"t", "c"} <= e.keys():
            EVENTOS.appendleft(e)

# ------------------------------------------------------------- configuración

class Config:
    def __init__(self):
        self.lock = threading.Lock()
        # copia propia de "fuentes": con dict() a secas se compartía el de
        # POR_DEFECTO y los ajustes de una cámara se colaban en otra instancia
        self.datos = {**POR_DEFECTO, "fuentes": {}}
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
            d["pais"] = pais_regulatorio()
        return d

    def fuente(self, ident):
        """Ajustes efectivos de una cámara: los suyos sobre los por defecto."""
        d = self.get()
        f = {"activa": True, "ndi_nombre": d["ndi_nombre"], "resolucion": d["resolucion"],
             "fps": d["fps"], "audio": d["audio"],
             "gpu_salida": "no", "gpu_extra": [], "gpu_kbps": 8000, "srt_puerto": 9000, "rtmp_url": "",
             "grabar_cuando": "siempre", "grabar_desde": "09:00", "grabar_hasta": "18:00",
             "grabar_manual": False, "formato": "auto", "latencia": "normal", "srt_latencia": 200,
             "retraso_audio": 0}
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

def ips_propias():
    return {d.split()[3].split("/")[0] for d in
            sh(["ip", "-o", "addr", "show"]).splitlines() if len(d.split()) > 3}

# ------------------------------------------------------------------ medición

ESTADO = {}
_nombres = {}

def nombre_de(ip):
    """Nombre mDNS de un receptor (cacheado: avahi tarda la primera vez)."""
    if ip not in _nombres:
        out = sh(["avahi-resolve", "-a", ip], timeout=3).split()
        _nombres[ip] = out[1].removesuffix(".local") if len(out) > 1 else None
    return _nombres[ip]

@cache_funcion(1.5)    # se pide desde varios hilos en cada ciclo de 2 s
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

def bytes_red(sentido):
    """Bytes enviados ("tx") o recibidos ("rx") por las interfaces de red."""
    total = 0
    for ruta in glob.glob(f"/sys/class/net/*/statistics/{sentido}_bytes"):
        if ruta.split("/")[4] not in ("lo", AP_IF):
            total += int(leer(ruta, "0"))
    return total

# --------------------------------------------------------------------- panel

def cliente_ip(handler):
    return handler.client_address[0].removeprefix("::ffff:")
