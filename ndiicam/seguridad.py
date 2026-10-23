# NDIICAM — acceso al panel: sesiones, límite de intentos y certificado HTTPS.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
import hashlib
import json
import os
import secrets
import socket
import ssl
import subprocess
import threading
import time

from nucleo import AP_IP, DATOS, cache_funcion, escribir_privado, evento

SESIONES = os.path.join(DATOS, "sesiones.json")
CERT = os.path.join(DATOS, "tls", "panel.crt")
LLAVE = os.path.join(DATOS, "tls", "panel.key")
COOKIE = "ndiicam_sesion"
DURACION_SESION = 30 * 24 * 3600        # se renueva sola con el uso
MAX_SESIONES = 50
CLAVE_MINIMA = 8


class Sesiones:
    """Sesiones del panel, guardadas como resumen SHA-256 del testigo (el
    testigo solo lo tiene la cookie del navegador). Sobreviven al reinicio del
    servicio: una actualización no obliga a volver a entrar."""

    def __init__(self, ruta=SESIONES):
        self.ruta = ruta
        self.lock = threading.Lock()
        self.datos = {}
        try:
            with open(ruta) as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.datos = {k: v for k, v in d.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    @staticmethod
    def _resumen(testigo):
        return hashlib.sha256(testigo.encode()).hexdigest()

    def crear(self, ip):
        testigo = secrets.token_urlsafe(32)
        ahora = time.time()
        with self.lock:
            self._purgar(ahora)
            if len(self.datos) >= MAX_SESIONES:     # la más antigua deja sitio
                del self.datos[min(self.datos, key=lambda k: self.datos[k]["usada"])]
            self.datos[self._resumen(testigo)] = {"creada": ahora, "usada": ahora, "ip": ip}
            self._guardar()
        return testigo

    def valida(self, testigo):
        if not testigo:
            return False
        clave = self._resumen(testigo)
        ahora = time.time()
        with self.lock:
            s = self.datos.get(clave)
            if not s or ahora - s["usada"] > DURACION_SESION:
                return False
            if ahora - s["usada"] > 3600:   # no escribir en disco en cada petición
                s["usada"] = ahora
                self._guardar()
            return True

    def cerrar(self, testigo):
        with self.lock:
            if self.datos.pop(self._resumen(testigo or ""), None):
                self._guardar()

    def cerrar_todas(self, salvo=None):
        """Al cambiar la contraseña: fuera todas menos la de quien la cambia."""
        guardar = self._resumen(salvo) if salvo else None
        with self.lock:
            self.datos = {k: v for k, v in self.datos.items() if k == guardar}
            self._guardar()

    def _purgar(self, ahora):
        self.datos = {k: v for k, v in self.datos.items() if ahora - v["usada"] <= DURACION_SESION}

    def _guardar(self):
        try:
            os.makedirs(os.path.dirname(self.ruta), exist_ok=True)
            escribir_privado(self.ruta + ".tmp", json.dumps(self.datos))
            os.replace(self.ruta + ".tmp", self.ruta)
        except OSError:
            pass


class LimiteIntentos:
    """Contraseñas fallidas: 5 seguidas desde una IP la bloquean 30 s, y cada
    tanda más el doble, hasta 15 min. Además, un tope de 30 fallos por minuto
    entre todas las IPs (un ataque repartido). Un acierto borra la cuenta."""

    LIBRES = 5
    BLOQUEO = 30
    BLOQUEO_MAX = 900
    TOPE_GLOBAL = 30

    def __init__(self, reloj=time.monotonic):
        self.reloj = reloj
        self.lock = threading.Lock()
        self.ips = {}           # ip -> [fallos, bloqueada_hasta]
        self.recientes = []     # instantes de los fallos, para el tope global

    def espera(self, ip):
        """Segundos que le quedan de bloqueo a esa IP (0 si puede probar)."""
        ahora = self.reloj()
        with self.lock:
            self.recientes = [t for t in self.recientes if ahora - t < 60]
            if len(self.recientes) >= self.TOPE_GLOBAL:
                return max(1, int(60 - (ahora - self.recientes[0])))
            hasta = self.ips.get(ip, [0, 0])[1]
            return max(0, int(hasta - ahora + 0.999))

    def fallo(self, ip):
        ahora = self.reloj()
        with self.lock:
            self.recientes.append(ahora)
            e = self.ips.setdefault(ip, [0, 0])
            e[0] += 1
            if e[0] >= self.LIBRES:
                tandas = e[0] - self.LIBRES
                e[1] = ahora + min(self.BLOQUEO_MAX, self.BLOQUEO * 2 ** tandas)
            if len(self.ips) > 1000:            # no crecer sin límite
                self.ips = dict(list(self.ips.items())[-500:])

    def acierto(self, ip):
        with self.lock:
            self.ips.pop(ip, None)


# ------------------------------------------------------------------- HTTPS

def nombres_certificado():
    host = socket.gethostname()
    return [f"DNS:{host}.local", f"DNS:{host}", f"IP:{AP_IP}"]


def vigencia_certificado(ruta=None):
    """(notBefore, notAfter) del certificado en segundos, o None si no se puede leer."""
    try:
        d = ssl._ssl._test_decode_cert(ruta or CERT)
        return ssl.cert_time_to_seconds(d["notBefore"]), ssl.cert_time_to_seconds(d["notAfter"])
    except (OSError, ValueError, KeyError, AttributeError, ssl.SSLError):
        return None


def certificado_vigente(ahora=None, margen=30 * 86400, ruta=None):
    """False si el certificado caduca en menos de 'margen' o aún no vale: pasa
    cuando se generó con el reloj de la placa mal (sin NTP) y luego se puso en
    hora; el navegador lo rechazaría para siempre."""
    v = vigencia_certificado(ruta)
    if v is None:
        return False
    ahora = time.time() if ahora is None else ahora
    return v[0] <= ahora and v[1] - ahora > margen


def asegurar_certificado():
    """Certificado propio del panel (10 años). Se rehace si cambia el nombre del
    equipo, si va a caducar o si no es válido todavía. Devuelve True si hay
    certificado utilizable."""
    marca = CERT + ".nombres"
    nombres = ",".join(nombres_certificado())
    try:
        with open(marca) as f:
            if f.read() == nombres and os.path.exists(LLAVE) and os.path.exists(CERT):
                if certificado_vigente():
                    return True
                evento("certificado_renovado")
    except OSError:
        pass
    os.makedirs(os.path.dirname(CERT), mode=0o700, exist_ok=True)
    host = socket.gethostname()
    try:
        r = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
             "-nodes", "-days", "3650", "-subj", f"/O=NDIICAM/CN={host}.local",
             "-addext", f"subjectAltName={nombres}", "-keyout", LLAVE + ".tmp", "-out", CERT + ".tmp"],
            capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return os.path.exists(CERT) and os.path.exists(LLAVE)
    if r.returncode:
        return os.path.exists(CERT) and os.path.exists(LLAVE)
    os.chmod(LLAVE + ".tmp", 0o600)
    os.replace(LLAVE + ".tmp", LLAVE)
    os.replace(CERT + ".tmp", CERT)
    with open(marca, "w") as f:
        f.write(nombres)
    return True


def contexto_tls():
    if not asegurar_certificado():
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(CERT, LLAVE)
    except (OSError, ssl.SSLError):
        return None
    return ctx


@cache_funcion(600)     # se pedía (y se hacía el hash) en cada /api/estado
def huella_certificado():
    """SHA-256 del certificado, para comprobarlo a mano en el aviso del navegador."""
    try:
        with open(CERT) as f:
            der = ssl.PEM_cert_to_DER_cert(f.read())
    except (OSError, ValueError):
        return None
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))
