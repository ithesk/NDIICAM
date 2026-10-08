#!/usr/bin/env python3
# NDIICAM — servidor de demostración para las capturas del README.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Sirve el panel real (ndiicam/web/index.html) con datos de ejemplo.

    python3 docs/screenshots/demo.py [puerto]
    python3 docs/screenshots/demo.py --estatico salida.html [es|en] [claro|oscuro] [panel|portal]

El modo --estatico escribe un HTML autónomo, con las respuestas de la API dentro,
que se abre sin servidor (así se hacen las capturas con Chrome sin ventana).

Parámetros de la URL:
    ?lang=es|en      idioma del panel
    ?tema=oscuro     fuerza el tema oscuro
    ?vista=portal    como lo ve un móvil desde la WiFi de configuración

Los datos son inventados: nada de una instalación real sale en las capturas.
"""
import json
import math
import os
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB = os.path.join(RAIZ, "ndiicam", "web", "index.html")
INICIO = time.time()
PASO = [0]          # cada consulta avanza la "hora" de las gráficas


def ruido(base, amplitud):
    return round(base + amplitud * math.sin(PASO[0] / 3) + random.uniform(-amplitud, amplitud) / 3, 1)


def fuente(ident, camara, modo, ancho_alto, fps, mbps, audio, receptores, nombre, res):
    return {
        "id": ident, "camara": camara, "dispositivo": "/dev/video0" if ident == "cam1" else "/dev/video2",
        "estado": "emitiendo", "error": "", "desde": INICIO - 2 * 3600 - 1260,
        "nombre_ndi": f"NDIICAM-01 ({nombre})", "modo": modo, "formato": "MJPG",
        "fps_camara": ruido(fps, 0.1), "fps_enviados": ruido(fps, 0.1), "mbps_camara": ruido(mbps, 3),
        "descartes": 0,
        "audio": {"dispositivo": audio, "activo": bool(audio), "fallido": False, "disponible": bool(audio)},
        "receptores": receptores, "hdmi": ident == "cam1", "hdmi_fallido": False,
        "ajustes": {"activa": True, "ndi_nombre": nombre, "resolucion": res, "fps": fps,
                    "audio": "auto"},
        "admite": {"720p": True, "1080p": True}, "fps_disponibles": [24, 25, 30],
    }


def estado(portal):
    ahora = time.time()
    fuentes = [
        fuente("cam1", "DJI Osmo Pocket 3", "1920×1080 @30", None, 30, 50, "DJI Osmo Pocket 3",
               [{"ip": "192.168.1.20", "nombre": "OBS-Studio"}, {"ip": "192.168.1.35", "nombre": "vMix-Control"}],
               "Stage", "1080p"),
        fuente("cam2", "Logitech HD Pro Webcam C920", "1280×720 @30", None, 30, 24, "HD Pro Webcam C920",
               [{"ip": "192.168.1.20", "nombre": "OBS-Studio"}], "Wide", "720p"),
    ]
    wifi = None if portal else {"ssid": "Studio-5G", "senal_dbm": -52, "senal_pct": 96, "banda": "5",
                                "canal": 149, "tx_mbit": 433.3, "rx_mbit": 390.0, "ip": "192.168.1.50"}
    eventos = [
        (5, "emitiendo", {"fuente": "Logitech HD Pro Webcam C920", "nombre": "Wide", "modo": "1280×720 @30",
                          "formato": "MJPG", "audio": True}),
        (9, "aplicando", {"fuente": "Logitech HD Pro Webcam C920"}),
        (12, "ajustes_fuente", {"fuente": "Logitech HD Pro Webcam C920", "campos": ["resolucion"]}),
        (300, "emitiendo", {"fuente": "DJI Osmo Pocket 3", "nombre": "Stage", "modo": "1920×1080 @30",
                            "formato": "MJPG", "audio": True}),
        (304, "cam_detectada", {"fuente": "DJI Osmo Pocket 3"}),
        (310, "cam_detectada", {"fuente": "Logitech HD Pro Webcam C920"}),
        (7600, "wifi_ok", {"ssid": "Studio-5G", "ip": "192.168.1.50"}),
        (7700, "ap_activa", {"ssid": "NDIICAM-4F2A", "modo": "simultaneo"}),
        (7720, "inicio", {"version": "0.1.0"}),
    ]
    if portal:
        eventos = [(20, "ap_activa", {"ssid": "NDIICAM-4F2A", "modo": "simultaneo"})] + eventos[3:]
    return {
        "version": "0.1.0",
        "equipo": {"nombre": "ndiicam-01", "uptime": ahora - INICIO + 3 * 86400 + 7 * 3600,
                   "cpu": round(ruido(64, 4)), "nucleos": 4, "potencia_w": ruido(1.9, 0.08),
                   "potencia_nucleos_w": ruido(0.9, 0.05), "temp": 58.0, "ram_pct": 22},
        "fuentes": fuentes,
        "audio_dispositivos": [{"id": "Pocket3", "nombre": "DJI Osmo Pocket 3", "usb": True},
                               {"id": "C920", "nombre": "HD Pro Webcam C920", "usb": True}],
        "emision": {"estado": "emitiendo", "emitiendo": 2, "activas": 2, "receptores": 3},
        "hdmi": {"activa": True, "fuente": "auto", "mostrando": "cam1",
                 "pantallas": [{"conector": "HDMI-A-1", "id": 95, "conectado": True, "modo": "1920x1080"}],
                 "consola": "1080p", "consola_activa": "1080p", "reinicio_pendiente": False},
        "red": {"modo": "sin_red" if portal else "wifi", "wifi": wifi, "ethernet": None, "tiene_wifi": True,
                "tx_mbps": 0.4 if portal else ruido(131, 6), "capacidad_mbps": None if portal else 217,
                "ip": None if portal else {
                    "perfil": "Studio-5G", "dispositivo": "wlan0", "metodo": "auto", "direccion": "",
                    "prefijo": 24, "gateway": "", "dns": [], "cambio": None,
                    "en_uso": {"direccion": "192.168.1.50", "prefijo": 24, "gateway": "192.168.1.1",
                               "dns": ["192.168.1.1"]}},
                "ap": {"activo": portal, "modo": "simultaneo" if portal else None, "ssid": "NDIICAM-4F2A",
                       "clave": "ndiicam-setup", "clientes": 1 if portal else 0, "simultaneo": True,
                       "forzado_hasta": None}},
        "actualizacion": {"actual": "0.1.0", "ultima": "v0.1.0", "disponible": False,
                          "comprobado": ahora - 3 * 3600, "error": None, "en_curso": False,
                          "url": "https://github.com/ithesk/NDIICAM/releases/tag/v0.1.0"},
        "config": {"transporte": "tcp", "ap_ssid": "NDIICAM-4F2A", "ap_clave": "ndiicam-setup",
                   "pais": "US", "clave_panel": False},
        "desde_ap": portal,
        "eventos": [{"t": ahora - s, "c": c, "d": d} for s, c, d in eventos],
        "conexion_wifi": None,
    }


REDES = [
    {"ssid": "Studio-5G", "senal": 92, "seguridad": "WPA2", "canal": "149", "banda": "5", "activa": False},
    {"ssid": "Studio", "senal": 84, "seguridad": "WPA2", "canal": "6", "banda": "2.4", "activa": False},
    {"ssid": "Control-Room", "senal": 61, "seguridad": "WPA2 WPA3", "canal": "36", "banda": "5", "activa": False},
    {"ssid": "Guest", "senal": 47, "seguridad": "", "canal": "11", "banda": "2.4", "activa": False},
]


class Demo(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _enviar(self, cuerpo, tipo):
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        portal = q.get("vista") == ["portal"]
        referer = parse_qs(urlparse(self.headers.get("Referer", "")).query)
        if url.path == "/":
            html = open(WEB, encoding="utf-8").read()
            previo = ""
            if "lang" in q:
                previo += f'try {{ localStorage.setItem("ndiicam-lang", "{q["lang"][0]}"); }} catch (_) {{}}\n'
            html = html.replace('<script>\n"use strict";', f'<script>\n"use strict";\n{previo}', 1)
            # para la captura: 30 consultas rápidas que llenan las gráficas y luego
            # parar, que Chrome sin ventana solo hace la foto cuando la página se calma
            html = html.replace("setTimeout(refrescar, 2000);",
                                "if ((window.vueltas = (window.vueltas || 0) + 1) < 30) setTimeout(refrescar, 60);", 1)
            if q.get("tema") == ["oscuro"]:
                html = html.replace("@media (prefers-color-scheme: dark)", "@media all", 1)
            if portal:
                html = html.replace("aplicarIdioma();\nrefrescar();",
                                    'aplicarIdioma();\nrefrescar();\nsetTimeout(() => $("btBuscar").click(), 800);')
            return self._enviar(html.encode(), "text/html; charset=utf-8")
        portal = portal or referer.get("vista") == ["portal"]
        if url.path == "/api/estado":
            PASO[0] += 1
            return self._enviar(json.dumps(estado(portal)).encode(), "application/json")
        if url.path == "/api/wifi/redes":
            guardadas = [] if portal else [
                {"id": "Studio-5G", "ssid": "Studio-5G", "prioridad": 40, "activa": True},
                {"id": "Studio", "ssid": "Studio", "prioridad": 20, "activa": False}]
            redes = [dict(r, activa=(r["ssid"] == "Studio-5G" and not portal)) for r in REDES]
            return self._enviar(json.dumps({"redes": redes, "guardadas": guardadas}).encode(),
                                "application/json")
        self.send_error(404)


def estatico(salida, lang="en", tema="claro", vista="panel"):
    portal = vista == "portal"
    estados = []
    for _ in range(30):
        PASO[0] += 1
        estados.append(estado(portal))
    redes = {"redes": [dict(r, activa=(r["ssid"] == "Studio-5G" and not portal)) for r in REDES],
             "guardadas": [] if portal else [
                 {"id": "Studio-5G", "ssid": "Studio-5G", "prioridad": 40, "activa": True},
                 {"id": "Studio", "ssid": "Studio", "prioridad": 20, "activa": False}]}
    # fetch local: cada consulta del panel recibe el siguiente estado de la lista
    falso = ("const DEMO_ESTADOS = " + json.dumps(estados) + ";\n"
             "const DEMO_REDES = " + json.dumps(redes) + ";\n"
             "window.fetch = async (ruta) => {\n"
             "  const d = ruta.startsWith('/api/wifi/redes') ? DEMO_REDES\n"
             "    : DEMO_ESTADOS[Math.min((window.vueltas || 0), DEMO_ESTADOS.length - 1)];\n"
             "  return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(d)) };\n"
             "};\n"
             f'try {{ localStorage.setItem("ndiicam-lang", "{lang}"); }} catch (_) {{}}\n')
    html = open(WEB, encoding="utf-8").read()
    html = html.replace('<script>\n"use strict";', '<script>\n"use strict";\n' + falso, 1)
    html = html.replace("setTimeout(refrescar, 2000);",
                        "if ((window.vueltas = (window.vueltas || 0) + 1) < 30) setTimeout(refrescar, 30);", 1)
    if tema == "oscuro":
        html = html.replace("@media (prefers-color-scheme: dark)", "@media all", 1)
    if portal:
        html = html.replace("aplicarIdioma();\nrefrescar();",
                            'aplicarIdioma();\nrefrescar();\nsetTimeout(() => $("btBuscar").click(), 300);')
    with open(salida, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--estatico":
        estatico(sys.argv[2], *sys.argv[3:])
        sys.exit()
    puerto = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"Demo en http://127.0.0.1:{puerto}/")
    ThreadingHTTPServer(("127.0.0.1", puerto), Demo).serve_forever()
