# NDIICAM — modo puente (como un ATEM Streaming Bridge): MediaMTX y los ajustes para el ATEM.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.request

from nucleo import API_MEDIAMTX, MEDIAMTX, PUERTO_RTMP, PUERTO_SRT, evento


class ServidorPuente:
    """MediaMTX, el servidor donde publica el ATEM en modo puente. Solo RTMP y
    SRT hacia fuera, la API en local, y una única ruta: live/<clave>; lo demás
    se rechaza. Se arranca al entrar en modo puente y se para al salir."""

    def __init__(self):
        self.proc = None
        self.clave = None
        self.reintento = 0.0

    def disponible(self):
        return os.access(MEDIAMTX, os.X_OK)

    def arrancar(self, clave):
        if self.proc is not None and self.proc.poll() is None and self.clave == clave:
            return
        if time.time() < self.reintento:
            return
        self.detener()
        os.makedirs("/run/ndiicam", exist_ok=True)
        conf = "/run/ndiicam/mediamtx.yml"
        with open(conf, "w") as f:
            f.write("# generado por NDIICAM (modo puente); se rehace en cada arranque\n"
                    "logLevel: warn\napi: yes\napiAddress: 127.0.0.1:9997\n"
                    "rtsp: no\nhls: no\nwebrtc: no\nmoq: no\nplayback: no\n"
                    f"rtmp: yes\nrtmpAddress: :{PUERTO_RTMP}\n"
                    f"srt: yes\nsrtAddress: :{PUERTO_SRT}\n"
                    f"paths:\n  live/{clave}: {{}}\n")
        self.clave = clave
        self.proc = subprocess.Popen([MEDIAMTX, conf], cwd="/run/ndiicam",
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.reintento = time.time() + 5     # si muere enseguida, no en bucle
        evento("puente_servidor", puerto=PUERTO_RTMP)

    def detener(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _api(self, ruta):
        try:
            with urllib.request.urlopen(API_MEDIAMTX + ruta, timeout=1) as r:
                return json.load(r)
        except (OSError, ValueError):
            return None

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
    configs = "".join(
        f'        <config resolution="1080p" fps="{fps}">\n'
        f"          <bitrate>{bps}</bitrate>\n"
        "          <audio-bitrate>128000</audio-bitrate>\n"
        "          <keyframe-interval>2</keyframe-interval>\n"
        "        </config>\n"
        for fps, bps in (("60", 12000000), ("50", 12000000), ("30", 8000000),
                         ("25", 8000000), ("24", 8000000)))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<streaming>\n  <service>\n'
            f"    <name>{nombre}</name>\n    <servers>\n      <server>\n"
            f"        <name>{socket.gethostname()}</name>\n"
            f"        <url>rtmp://{ip}:{PUERTO_RTMP}/live</url>\n"
            "      </server>\n    </servers>\n"
            f"    <key>{clave}</key>\n"
            "    <profiles>\n      <profile>\n        <name>NDIICAM</name>\n"
            f"{configs}      </profile>\n    </profiles>\n  </service>\n</streaming>\n")
