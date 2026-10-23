# NDIICAM — modo puente (como un ATEM Streaming Bridge): MediaMTX y los ajustes para el ATEM.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
import json
import os
import re
import secrets
import socket
import subprocess
import time
import urllib.request

from nucleo import API_MEDIAMTX, MEDIAMTX, PUERTO_HLS, PUERTO_RTMP, PUERTO_SRT, RUTA_PROGRAMA, evento


def conf_mediamtx(clave, reparto=False):
    """Configuración de MediaMTX. El ATEM publica en live/<clave>, la única ruta que
    se puede publicar desde fuera. Con reparto (edición Pro) hay además una ruta
    'programa', que solo publica este equipo (el programa final, con el audio de la
    consola: ver programa.py), y HLS para navegadores, móviles y Smart TV. Leer se
    puede desde cualquier sitio: es lo que se reparte."""
    conf = ("# generado por NDIICAM (modo puente); se rehace en cada arranque\n"
            "logLevel: warn\napi: yes\napiAddress: 127.0.0.1:9997\n"
            "rtsp: no\nwebrtc: no\nmoq: no\nplayback: no\n"
            f"rtmp: yes\nrtmpAddress: :{PUERTO_RTMP}\n"
            f"srt: yes\nsrtAddress: :{PUERTO_SRT}\n")
    if reparto:
        conf += f"hls: yes\nhlsAddress: :{PUERTO_HLS}\nhlsAllowOrigins: ['*']\n"
    else:
        conf += "hls: no\n"
    conf += ("authInternalUsers:\n"
             "- user: any\n  pass:\n  ips: ['127.0.0.1', '::1']\n  permissions:\n"
             "  - action: publish\n  - action: read\n  - action: api\n"
             "- user: any\n  pass:\n  ips: []\n  permissions:\n"
             f"  - action: publish\n    path: live/{clave}\n  - action: read\n")
    conf += f"paths:\n  live/{clave}: {{}}\n"
    if reparto:
        conf += f"  {RUTA_PROGRAMA}: {{}}\n"
    return conf


def usuarios_rutas(ingestas):
    """Los permisos de MediaMTX con las rutas (lo que va en authInternalUsers): este
    equipo publica y lee lo que quiera; desde la red solo se puede publicar en las
    ingestas (live/<clave> de un ATEM, OBS, un móvil), y leer todo: es lo que se
    reparte. En el formato de la API, para aplicarlo en caliente."""
    local = {"user": "any", "pass": "", "ips": ["127.0.0.1", "::1"],
             "permissions": [{"action": "publish"}, {"action": "read"}, {"action": "api"}]}
    permisos = []
    claves = sorted({c.split("/", 1)[1] for c in ingestas if c.startswith("live/")})
    if claves:
        permisos.append({"action": "publish",
                         "path": "~^live/(" + "|".join(re.escape(k) for k in claves) + ")$"})
    permisos.append({"action": "read"})
    return [local, {"user": "any", "pass": "", "ips": [], "permissions": permisos}]


def conf_mediamtx_rutas(ingestas, publicaciones=(), hls=True):
    """MediaMTX como bus de las rutas. ingestas: caminos donde publican de fuera (un
    ATEM, OBS, un móvil: live/<clave>), los únicos que se pueden publicar desde la
    red. Los programas de las rutas (live/<camino>) los publica este equipo. Leer se
    puede todo y desde cualquier sitio (OBS por RTMP, la TV y el móvil por HLS, vMix
    y VLC por SRT).

    Las rutas se declaran por expresión regular (~^live/.*$): así la configuración no
    cambia al añadir, quitar o renombrar rutas y MediaMTX no se reinicia (un reinicio
    corta al ATEM, a OBS y las grabaciones). Solo cambia con las claves de las
    ingestas, y eso se aplica por la API, también sin reiniciar."""
    conf = ("# generado por NDIICAM (rutas); se rehace cuando cambian\n"
            "logLevel: warn\napi: yes\napiAddress: 127.0.0.1:9997\n"
            "rtsp: no\nwebrtc: no\nmoq: no\nplayback: no\n"
            f"rtmp: yes\nrtmpAddress: :{PUERTO_RTMP}\n"
            f"srt: yes\nsrtAddress: :{PUERTO_SRT}\n")
    conf += (f"hls: yes\nhlsAddress: :{PUERTO_HLS}\nhlsAllowOrigins: ['*']\n" if hls else "hls: no\n")
    conf += "authInternalUsers:\n"
    for u in usuarios_rutas(ingestas):
        conf += (f"- user: {u['user']}\n  pass:\n  ips: [{', '.join(repr(i) for i in u['ips'])}]\n"
                 "  permissions:\n")
        for pm in u["permissions"]:
            conf += f"  - action: {pm['action']}\n"
            if pm.get("path"):
                conf += f"    path: '{pm['path']}'\n"
    conf += "paths:\n  '~^live/.*$': {}\n"
    return conf


class ServidorPuente:
    """MediaMTX, el servidor donde publica el ATEM en modo puente. Solo RTMP y
    SRT hacia fuera, la API en local, y una única ruta: live/<clave>; lo demás
    se rechaza. Se arranca al entrar en modo puente y se para al salir."""

    def __init__(self):
        self.proc = None
        self.clave = None
        self.conf = None
        self.reparto = False
        self.reintento = 0.0
        self.usuarios = None    # permisos aplicados (rutas), para cambiarlos en caliente
        self._cache = {}        # camino -> (hora, respuesta de /v3/paths/get)

    def disponible(self):
        return os.access(MEDIAMTX, os.X_OK)

    def arrancar_conf(self, conf, usuarios=None):
        """Con una configuración dada (la de las rutas). Si MediaMTX ya corre y solo
        cambian los permisos (otra ingesta), se aplican por la API sin reiniciarlo:
        reiniciar corta al ATEM, a OBS, a la TV y las grabaciones. Solo se rehace
        si no corre o si la API no contesta."""
        vivo = self.proc is not None and self.proc.poll() is None
        if vivo and self.conf == conf:
            return
        if vivo and usuarios is not None and self._api(
                "/v3/config/global/patch", {"authInternalUsers": usuarios}) is not None:
            self.conf, self.usuarios = conf, usuarios
            return
        if time.time() < self.reintento:
            return
        self.detener()
        os.makedirs("/run/ndiicam", exist_ok=True)
        ruta = "/run/ndiicam/mediamtx.yml"
        # con las claves de las ingestas dentro: que no lo lea cualquier usuario local
        with os.fdopen(os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(conf)
        self.conf, self.usuarios, self.clave = conf, usuarios, None
        self.proc = subprocess.Popen([MEDIAMTX, ruta], cwd="/run/ndiicam",
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.reintento = time.time() + 5
        evento("puente_servidor", puerto=PUERTO_RTMP)

    def _camino(self, camino):
        """/v3/paths/get de un camino, guardado 1,5 s: el panel, el medidor y el
        supervisor lo piden a la vez, y MediaMTX no tiene por qué contestar a todos."""
        ahora = time.monotonic()
        t, r = self._cache.get(camino, (0.0, None))
        if ahora - t > 1.5:
            r = self._api(f"/v3/paths/get/{camino}")
            self._cache[camino] = (ahora, r)
        return r

    def ruta_info(self, camino):
        """Si alguien publica en ese camino, desde dónde, qué manda y cuántos leen."""
        if self.proc is None or self.proc.poll() is not None:
            return {"activo": False, "publicando": False}
        r = self._camino(camino)
        if not r or not r.get("ready"):
            return {"activo": True, "publicando": False}
        fuente = r.get("source") or {}
        res = {"activo": True, "publicando": True, "pistas": r.get("tracks") or [],
               "protocolo": {"rtmpConn": "RTMP", "srtConn": "SRT"}.get(fuente.get("type"), fuente.get("type")),
               "lectores": len(r.get("readers") or [])}
        lista = {"rtmpConn": "rtmpconns", "srtConn": "srtconns"}.get(fuente.get("type"))
        if lista and fuente.get("id"):
            con = self._api(f"/v3/{lista}/get/{fuente['id']}") or {}
            res["desde"] = (con.get("remoteAddr") or "").rsplit(":", 1)[0] or None
        return res

    def arrancar(self, clave, reparto=False):
        if (self.proc is not None and self.proc.poll() is None and self.clave == clave
                and self.reparto == reparto):
            return
        if time.time() < self.reintento:
            return
        self.detener()
        os.makedirs("/run/ndiicam", exist_ok=True)
        conf = "/run/ndiicam/mediamtx.yml"
        with open(conf, "w") as f:
            f.write(conf_mediamtx(clave, reparto))
        self.clave, self.reparto, self.conf = clave, reparto, None
        self.proc = subprocess.Popen([MEDIAMTX, conf], cwd="/run/ndiicam",
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.reintento = time.time() + 5     # si muere enseguida, no en bucle
        evento("puente_servidor", puerto=PUERTO_RTMP)

    def detener(self):
        proc, self.proc = self.proc, None
        self.conf = self.usuarios = None
        self._cache = {}
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _api(self, ruta, datos=None):
        """GET, o PATCH con datos (JSON). None si falla; {} si contesta sin cuerpo.
        Con 3 s de margen: con el equipo cargado MediaMTX tarda en contestar, y un
        retraso aquí no puede parar una grabación."""
        try:
            req = urllib.request.Request(API_MEDIAMTX + ruta)
            if datos is not None:
                req = urllib.request.Request(API_MEDIAMTX + ruta, data=json.dumps(datos).encode(),
                                             headers={"Content-Type": "application/json"},
                                             method="PATCH")
            with urllib.request.urlopen(req, timeout=3) as r:
                cuerpo = r.read()
                return json.loads(cuerpo) if cuerpo.strip() else {}
        except (OSError, ValueError):
            return None

    def ruta_lista(self, ruta):
        """Si alguien está publicando en esa ruta (el programa final, por ejemplo)."""
        if self.proc is None or self.proc.poll() is not None:
            return False
        r = self._camino(ruta)
        return bool(r and r.get("ready"))

    def info(self):
        """Quién publica (ATEM u otro), desde dónde y qué manda."""
        if self.proc is None or self.proc.poll() is not None:
            return {"activo": False}
        ruta = self._api(f"/v3/paths/get/live/{self.clave}")
        res = {"activo": True, "publicando": False}
        if not ruta or not ruta.get("ready"):
            return res
        fuente = ruta.get("source") or {}
        res.update(publicando=True, protocolo={"rtmpConn": "RTMP", "srtConn": "SRT"}.get(
            fuente.get("type"), fuente.get("type")), pistas=ruta.get("tracks") or [],
            bytes=ruta.get("bytesReceived", 0))
        lista = {"rtmpConn": "rtmpconns", "srtConn": "srtconns"}.get(fuente.get("type"))
        if lista and fuente.get("id"):
            con = self._api(f"/v3/{lista}/get/{fuente['id']}") or {}
            res["desde"] = (con.get("remoteAddr") or "").rsplit(":", 1)[0] or None
        return res

PUENTE = ServidorPuente()

def clave_puente(config):
    """La clave del puente; se genera la primera vez."""
    clave = config.get()["puente_clave"]
    if not clave:
        clave = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
        config.actualizar({"puente_clave": clave})
    return clave

def xml_atem(ip, clave):
    """Ajustes de emisión para ATEM Software Control (Stream > Load Streaming
    Settings): el ATEM publica en este equipo como en un Streaming Bridge."""
    nombre = f"NDIICAM {socket.gethostname()}"

    def perfil(titulo, tasas, audio):
        configs = "".join(
            f'        <config resolution="1080p" fps="{fps}">\n'
            f"          <bitrate>{bps}</bitrate>\n"
            f"          <audio-bitrate>{audio}</audio-bitrate>\n"
            # un fotograma clave por segundo: el puente (RTMP) empieza a mostrar desde
            # el último, así que con 1 s se recorta hasta un segundo de retardo; y al
            # editar la grabación se puede cortar en cualquier segundo
            "          <keyframe-interval>1</keyframe-interval>\n"
            "        </config>\n"
            for fps, bps in tasas)
        return f"      <profile>\n        <name>{titulo}</name>\n{configs}      </profile>\n"
    # el de siempre, y otro con más calidad para grabar y editar después (la
    # grabación del programa copia lo que manda el ATEM, sin recodificar: no puede
    # mejorarlo). En la red local cabe de sobra; si un ATEM no aguanta la tasa
    # alta, queda el otro
    perfiles = (perfil("NDIICAM", (("60", 12000000), ("50", 12000000), ("30", 8000000),
                                   ("25", 8000000), ("24", 8000000)), 128000)
                + perfil("NDIICAM edición", (("60", 25000000), ("50", 25000000), ("30", 20000000),
                                            ("25", 20000000), ("24", 20000000)), 256000))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<streaming>\n  <service>\n'
            f"    <name>{nombre}</name>\n    <servers>\n      <server>\n"
            f"        <name>{socket.gethostname()}</name>\n"
            f"        <url>rtmp://{ip}:{PUERTO_RTMP}/live</url>\n"
            "      </server>\n    </servers>\n"
            f"    <key>{clave}</key>\n"
            f"    <profiles>\n{perfiles}    </profiles>\n  </service>\n</streaming>\n")
