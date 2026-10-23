#!/usr/bin/env python3
# NDIICAM — panel de demostración para las capturas del README.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Escribe el panel real (ndiicam/web/index.html) como un HTML autónomo, con las
respuestas de la API dentro, que se abre sin servidor: así se hacen las capturas
con Chrome sin ventana.

    python3 docs/screenshots/demo.py salida.html [es|en] [claro|oscuro] [vista]

Vistas:
    monitor    pestaña Monitor: dos cámaras emitiendo (una grabando por GPU, otra en el aire)
    config     pestaña Configuración
    sistema    pestaña Sistema: seguridad, equipo y registro
    entrada    la pantalla que pide la contraseña
    portal     como lo ve un móvil desde la WiFi de configuración
    receptor   el equipo en modo receptor, mostrando una fuente NDI por HDMI
    rutas      pestaña Rutas (lienzo): el culto con ATEM, consola, una cámara y el multiview
    rutas-lista  la misma, en la vista lista (la del móvil)
    rutas-monitor  pestaña Monitor con el motor de rutas

Los datos son inventados: nada de una instalación real sale en las capturas.
Las vistas previas de las cámaras se dibujan aquí mismo (Pillow).
"""
import base64
import io
import json
import math
import os
import random
import sys
import time

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB = os.path.join(RAIZ, "ndiicam", "web", "index.html")
VERSION = "0.5.0"
AHORA = time.time()
random.seed(7)


def ruido(paso, base, amplitud):
    return round(base + amplitud * math.sin(paso / 3) + random.uniform(-amplitud, amplitud) / 3, 1)


# ------------------------------------------------------------ vistas previas

def preview(escena):
    """Una imagen de cámara inventada, como data: URI."""
    from PIL import Image, ImageDraw, ImageFilter
    w, h = 640, 360
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    if escena == "escenario":
        for y in range(h):              # fondo oscuro con un poco de azul
            v = y / h
            d.line([(0, y), (w, y)], fill=(int(14 + 20 * v), int(16 + 18 * v), int(34 + 30 * v)))
        luz = Image.new("L", (w, h), 0)     # cono de luz cálida
        ImageDraw.Draw(luz).polygon([(300, -20), (340, -20), (470, 330), (170, 330)], fill=150)
        luz = luz.filter(ImageFilter.GaussianBlur(40))
        im.paste(Image.new("RGB", (w, h), (255, 196, 120)), (0, 0), luz)
        d = ImageDraw.Draw(im)
        d.rectangle([0, 300, w, h], fill=(38, 28, 24))                 # tarima
        d.ellipse([200, 300, 440, 336], fill=(120, 86, 56))            # suelo iluminado
        d.polygon([(286, 210), (354, 210), (346, 305), (294, 305)], fill=(52, 40, 34))   # atril
        d.ellipse([304, 132, 336, 166], fill=(196, 150, 116))           # cabeza
        d.polygon([(290, 168), (350, 168), (360, 214), (280, 214)], fill=(46, 58, 92))   # cuerpo
        for _ in range(26):                                             # luces desenfocadas
            x, y, r = random.randint(0, w), random.randint(0, 120), random.randint(6, 16)
            d.ellipse([x - r, y - r, x + r, y + r], fill=random.choice([(90, 70, 160), (60, 110, 190), (170, 90, 140)]))
    else:
        for y in range(h):              # sala: pared clara y suelo
            v = y / h
            c = (int(222 - 40 * v), int(226 - 40 * v), int(232 - 34 * v)) if y < 220 else (120, 104, 92)
            d.line([(0, y), (w, y)], fill=c)
        d.rectangle([200, 40, 440, 175], fill=(26, 40, 70))             # pantalla
        d.rectangle([210, 50, 430, 165], fill=(52, 102, 210))
        d.rectangle([232, 78, 330, 92], fill=(220, 232, 255))
        d.rectangle([232, 102, 400, 110], fill=(160, 190, 240))
        d.rectangle([232, 118, 380, 126], fill=(160, 190, 240))
        for fila, (y0, tam) in enumerate([(230, 26), (265, 32), (305, 40)]):    # filas de público
            for i in range(9 + fila):
                x = 20 + i * (w - 40) / (8 + fila) + (fila % 2) * 18
                color = random.choice([(70, 60, 58), (40, 52, 80), (110, 70, 60), (60, 80, 70)])
                d.ellipse([x - tam / 2, y0 - tam, x + tam / 2, y0], fill=(150, 112, 90))
                d.rectangle([x - tam * 0.75, y0 - 2, x + tam * 0.75, y0 + tam], fill=color)
    im = im.filter(ImageFilter.GaussianBlur(1.2))
    if escena == "multiview":
        # cuatro entradas abajo, previo y programa arriba con sus marcos
        im = Image.new("RGB", (w, h), (12, 12, 14))
        d = ImageDraw.Draw(im)
        for i, c in enumerate([(40, 60, 110), (70, 50, 40), (30, 80, 60), (90, 70, 120)]):
            x0 = 4 + i * 158
            d.rectangle([x0, 186, x0 + 154, 272], fill=c)
            d.rectangle([x0, 276, x0 + 154, 356], fill=tuple(int(v * 0.7) for v in c))
        d.rectangle([4, 4, 316, 180], fill=(60, 50, 80)); d.rectangle([4, 4, 316, 180], outline=(40, 200, 90), width=4)
        d.rectangle([324, 4, 636, 180], fill=(38, 30, 46)); d.rectangle([324, 4, 636, 180], outline=(230, 50, 50), width=4)
        d.ellipse([440, 60, 520, 140], fill=(196, 150, 116))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


# --------------------------------------------------------------------- datos

def fuente(ident, camara, nombre, modo, fps, mbps, audio, receptores, res, gpu=None, tally=None, nivel=-18):
    g = gpu or {}
    return {
        "id": ident, "camara": camara, "dispositivo": "/dev/video0" if ident == "cam1" else "/dev/video2",
        "estado": "emitiendo", "error": "", "desde": AHORA - 2 * 3600 - 1260,
        "nombre_ndi": f"NDIICAM-01 ({nombre})", "modo": modo, "formato": "MJPG", "hilos": 1,
        "fps_camara": fps, "fps_enviados": fps, "mbps_camara": mbps, "descartes": 0,
        "audio": {"dispositivo": audio, "activo": bool(audio), "nivel": {"peak": nivel + 6, "rms": nivel} if audio else None,
                  "fallido": False, "disponible": bool(audio)},
        "receptores": receptores, "conexiones_ndi": len(receptores),
        "latencia_ms": 68 if ident == "cam1" else 101, "tally": tally or {"programa": False, "previo": False},
        "hdmi": ident == "cam1", "hdmi_fallido": False,
        "gpu": {"modo": g.get("modo", "no"), "puerto": None, "estado": "activa" if gpu else None, "error": None,
                "fps": fps if gpu else 0, "mbps": 8.1 if gpu else 0},
        "ajustes": {"activa": True, "ndi_nombre": nombre, "resolucion": res, "fps": 30, "audio": "auto",
                    "gpu_salida": g.get("modo", "no"), "gpu_kbps": 8000, "srt_puerto": 9000, "rtmp_url": "",
                    "grabar_cuando": "manual" if gpu else "siempre", "grabar_desde": "09:00",
                    "grabar_hasta": "18:00", "grabar_manual": bool(gpu), "formato": "auto",
                    "latencia": "baja" if ident == "cam1" else "normal", "srt_latencia": 200},
        "admite": {"720p": True, "1080p": True}, "fps_disponibles": [24, 25, 30],
    }


def estado(paso, vista, lang="es"):
    if vista.startswith("rutas"):
        return estado_rutas(paso, lang)
    portal, receptor = vista == "portal", vista == "receptor"
    fuentes = [
        fuente("cam1", "DJI Osmo Pocket 3", "Stage", "1920×1080 @30", ruido(paso, 30, 0.1), ruido(paso, 50, 3),
               "DJI Osmo Pocket 3", [{"ip": "192.168.1.20", "nombre": "OBS-Studio"},
                                     {"ip": "192.168.1.35", "nombre": "vMix-Control"}],
               "1080p", gpu={"modo": "grabar"}, tally={"programa": True, "previo": False},
               nivel=ruido(paso, -16, 4)),
        fuente("cam2", "Logitech HD Pro Webcam C920", "Wide", "1280×720 @30", ruido(paso, 30, 0.1),
               ruido(paso, 24, 2), "HD Pro Webcam C920", [{"ip": "192.168.1.20", "nombre": "OBS-Studio"}],
               "720p", tally={"programa": False, "previo": True}, nivel=ruido(paso, -24, 3)),
    ]
    wifi = None if portal else {"ssid": "Studio-5G", "senal_dbm": -52, "senal_pct": 96, "banda": "5", "canal": 149,
                                "tx_mbit": 433.3, "rx_mbit": 390.0, "ip": "192.168.1.50", "mac": "02:00:00:4f:2a:01"}
    eventos = [
        (40, "grabacion_manual", {"fuente": "DJI Osmo Pocket 3", "grabar": True}),
        (65, "tally", {"fuente": "DJI Osmo Pocket 3", "programa": True, "previo": False}),
        (300, "emitiendo", {"fuente": "Logitech HD Pro Webcam C920", "nombre": "Wide", "modo": "1280×720 @30",
                            "formato": "MJPG", "audio": True}),
        (302, "gpu_activa", {"fuente": "DJI Osmo Pocket 3", "modo": "grabar"}),
        (304, "emitiendo", {"fuente": "DJI Osmo Pocket 3", "nombre": "Stage", "modo": "1920×1080 @30",
                            "formato": "MJPG", "audio": True}),
        (310, "cam_detectada", {"fuente": "DJI Osmo Pocket 3"}),
        (312, "cam_detectada", {"fuente": "Logitech HD Pro Webcam C920"}),
        (316, "cortafuegos", {"detalle": "emisor"}),
        (318, "arranque", {"motivo": "reinicio_panel", "detalle": None}),
        (320, "inicio", {"version": VERSION}),
    ]
    if portal:
        eventos = [(20, "ap_activa", {"ssid": "NDIICAM-4F2A", "modo": "simultaneo"})] + eventos[2:]
    rx = None
    if receptor:
        for f in fuentes:
            f.update(estado="pausada", nombre_ndi=None, fps_camara=0, fps_enviados=0, mbps_camara=0,
                     receptores=[], hdmi=False, latencia_ms=None, tally=None, conexiones_ndi=None)
            f["gpu"].update(estado=None, fps=0, mbps=0)
            f["ajustes"].update(gpu_salida="no", grabar_manual=False)
            f["audio"]["nivel"] = None
        rx = {"fuente": "STUDIO-PC (OBS Program)", "estado": "recibiendo", "error": "", "desde": AHORA - 1260,
              "ancho": 1920, "alto": 1080, "fps_fuente": 59.94, "fps": ruido(paso, 59.9, 0.1), "audio": True,
              "audio_activo": True, "nivel": {"peak": ruido(paso, -12, 4), "rms": ruido(paso, -18, 4)},
              "latencia_ms": 48, "audio_fallido": False}
        eventos = [(1260, "rx_recibiendo", {"fuente": "STUDIO-PC (OBS Program)", "modo": "1920×1080 @59.94"}),
                   (1266, "ajustes", {"campos": ["rx_fuente"]}),
                   (1270, "modo", {"modo": "receptor"})] + eventos
    gpu_ocupada = 0 if receptor else ruido(paso, 11, 2)
    return {
        "version": VERSION,
        "gpu": {"disponible": True, "motivo": "ok", "codificador": "vah264lpenc"},
        "equipo": {"nombre": "ndiicam-01", "uptime": 3 * 86400 + 7 * 3600 + paso * 2,
                   "cpu": round(ruido(paso, 24 if receptor else 58, 3)), "nucleos": 4,
                   "potencia_w": ruido(paso, 1.3 if receptor else 2.4, 0.08),
                   "potencia_nucleos_w": ruido(paso, 0.9, 0.05),
                   "gpu": {"ocupada": gpu_ocupada, "motores": {"render": 0, "copy": 0, "video": gpu_ocupada,
                                                               "video-enhance": 0},
                           "mhz": 300 if gpu_ocupada else 100, "mhz_max": 700, "w": 0.3 if gpu_ocupada else 0},
                   "temp": 58.0, "ram_pct": 22,
                   "watchdog": {"activo": True, "dispositivo": "wdat_wdt", "timeout": 30},
                   "hora": {"zona": "Europe/Madrid", "sincronizada": True, "ntp": True},
                   "arranque": {"codigo": "reinicio_panel", "ultima_senal": AHORA - 3 * 86400 - 7 * 3600 - 40,
                                "detalle": None, "t": AHORA - 3 * 86400 - 7 * 3600}},
        "fuentes": fuentes,
        "audio_dispositivos": [{"id": "Pocket3", "nombre": "DJI Osmo Pocket 3", "usb": True},
                               {"id": "C920", "nombre": "HD Pro Webcam C920", "usb": True}],
        "modo": "receptor" if receptor else "emisor",
        "puente": None,
        "receptor": {"fuente": rx["fuente"] if rx else "", "estado": rx,
                     "red_ndi": [{"nombre": "STUDIO-PC (OBS Program)", "url": "192.168.1.20:5961"},
                                 {"nombre": "STUDIO-PC (OBS Preview)", "url": "192.168.1.20:5962"},
                                 {"nombre": "VMIX-CONTROL (Output 1)", "url": "192.168.1.35:5961"}]
                     if receptor else []},
        "hdmi": {"activa": True, "fuente": "auto", "mostrando": None if receptor else "cam1",
                 "pantallas": [{"conector": "HDMI-A-1", "id": 95, "conectado": True, "modo": "1920x1080"}],
                 "consola": "1080p", "consola_activa": "1080p", "reinicio_pendiente": False},
        "emision": {"estado": "recibiendo", "emitiendo": 0, "activas": 2, "receptores": 0} if receptor
        else {"estado": "emitiendo", "emitiendo": 2, "activas": 2, "receptores": 3},
        "red": {"modo": "sin_red" if portal else "wifi", "wifi": wifi,
                "ethernet": {"conectada": False, "ip": None, "velocidad": None, "dev": "enp1s0",
                             "mac": "02:00:00:4f:2a:02",
                             "ip_config": {"perfil": "Wired", "dispositivo": "enp1s0", "conectado": False,
                                           "metodo": "auto", "direccion": "", "prefijo": 24, "gateway": "",
                                           "dns": []}},
                "tiene_wifi": True,
                "tx_mbps": 0.4 if portal or receptor else ruido(paso, 131, 6),
                "rx_mbps": ruido(paso, 160, 6) if receptor else 0.3,
                "trafico_mbps": ruido(paso, 160, 6) if receptor else (0.4 if portal else ruido(paso, 131, 6)),
                "sentido": "entrada" if receptor else "salida",
                "capacidad_mbps": None if portal else 217,
                "ip": None if portal else {
                    "perfil": "Studio-5G", "dispositivo": "wlan0", "metodo": "auto", "direccion": "",
                    "prefijo": 24, "gateway": "", "dns": [], "cambio": None,
                    "en_uso": {"direccion": "192.168.1.50", "prefijo": 24, "gateway": "192.168.1.1",
                               "dns": ["192.168.1.1"]}},
                "ap": {"activo": portal, "modo": "simultaneo" if portal else None, "ssid": "NDIICAM-4F2A",
                       "clave": "studio-setup-24", "clientes": 1 if portal else 0, "simultaneo": True,
                       "forzado_hasta": None}},
        "actualizacion": {"actual": VERSION, "ultima": "v" + VERSION, "disponible": False,
                          "comprobado": AHORA - 3 * 3600, "error": None, "en_curso": False,
                          "url": f"https://github.com/ithesk/NDIICAM/releases/tag/v{VERSION}"},
        "config": {"transporte": "tcp", "ap_ssid": "NDIICAM-4F2A", "ap_clave": "studio-setup-24", "pais": "ES",
                   "idioma": "es", "clave_panel": True, "decodificadores": "auto", "cortafuegos": True,
                   "puertos_extra": ""},
        "desde_ap": portal,
        "eventos": [{"t": AHORA - s, "c": c, "d": d} for s, c, d in eventos],
        "conexion_wifi": None,
        "seguridad": {"https": not portal, "https_disponible": True,
                      "huella": "7C:41:9E:02:B3:5D:C8:16:F0:2A:9B:64:E1:73:0D:5F:A8:C2:39:4E:B7:11:6D:FA:28:93:C5:0E:7B:D4:62:A1",
                      "ap_clave_defecto": False,
                      "cortafuegos": {"activado": True, "activo": True, "error": None,
                                      "abiertos": ["tcp/22", "tcp/53", "tcp/80", "tcp/443", "udp/53", "udp/67",
                                                   "udp/68", "udp/546", "udp/5353", "tcp/5959-5999",
                                                   "udp/5959-5999"]}},
    }


NOMBRES = {"es": {"culto": "Culto", "cam2": "Cámara 2", "operador": "Operador", "pulpito": "Púlpito", "consola": "Consola X32"},
           "en": {"culto": "Service", "cam2": "Camera 2", "operador": "Operator", "pulpito": "Pulpit", "consola": "X32 console"}}


def rutas_demo(paso, lang="es"):
    """/api/rutas de ejemplo: un culto con un ATEM que llega por SRT, el audio de la
    consola por USB, una cámara con su micro y el multiview del ATEM al monitor."""
    n = NOMBRES[lang]
    entradas = [
        {"id": "atem", "tipo": "ingesta", "nombre": "ATEM Mini Pro", "clave": "Qm7kT2xR9a",
         "llega": {"activo": True, "publicando": True, "protocolo": "SRT", "desde": "192.168.1.40", "lectores": 3}},
        {"id": "audio:X32", "tipo": "usb_audio", "nombre": n["consola"], "dev": "plughw:CARD=X32,DEV=0"},
        {"id": "usb-pocket", "tipo": "usb_cam", "nombre": "DJI Osmo Pocket 3", "dev": "/dev/video0",
         "audio_dev": "plughw:CARD=Pocket3,DEV=0"},
        {"id": "usb-mview", "tipo": "usb_cam", "nombre": "ATEM multiview", "dev": "/dev/video2", "audio_dev": None},
    ]
    rutas = [
        {"id": "culto", "nombre": n["culto"], "camino": n["culto"].lower(), "video": "atem", "audio": "audio:X32",
         "ganancia": -3, "retraso": 480, "mudo": False, "activa": True,
         "salidas": [{"id": "culto-obs", "tipo": "obs"}, {"id": "culto-ndi", "tipo": "ndi", "nombre": n["culto"]},
                     {"id": "culto-grabar", "tipo": "grabar", "formato": "mkv"},
                     {"id": "culto-yt", "tipo": "rtmp", "url": "rtmp://a.rtmp.youtube.com/live2/xxxx-xxxx-xxxx"}]},
        {"id": "cam2", "nombre": n["cam2"], "camino": n["cam2"].lower().replace("á", "a").replace(" ", "-"), "video": "usb-pocket", "audio": "usb-pocket",
         "ganancia": 0, "retraso": 0, "mudo": False, "activa": True,
         "salidas": [{"id": "cam2-ndi", "tipo": "ndi", "nombre": n["pulpito"]}]},
        {"id": "operador", "nombre": n["operador"], "camino": n["operador"].lower(), "video": "usb-mview", "audio": "",
         "ganancia": 0, "retraso": 0, "mudo": False, "activa": True,
         "salidas": [{"id": "operador-hdmi", "tipo": "hdmi"}]},
    ]
    pico = ruido(paso, -14, 4)
    estado = {
        "culto": {"estado": "en_marcha", "errores": [], "lectores": 3, "fps": 30.0, "copia": True,
                  "audio_db": {"peak": pico, "rms": pico - 6}, "calibracion": None,
                  "salidas": {"culto-obs": "ok", "culto-ndi": "ok", "culto-grabar": "ok", "culto-yt": "ok"}},
        "cam2": {"estado": "en_marcha", "errores": [], "lectores": 0, "fps": 30.0, "copia": False,
                 "audio_db": {"peak": pico - 6, "rms": pico - 12}, "calibracion": None, "salidas": {"cam2-ndi": "ok"}},
        "operador": {"estado": "en_marcha", "errores": [], "lectores": 0, "fps": 60.0, "copia": False,
                     "audio_db": None, "calibracion": None, "salidas": {"operador-hdmi": "ok"}},
    }
    return {"motor": "rutas", "entradas": entradas, "rutas": rutas, "errores": {}, "estado": estado,
            "activas": ["culto", "cam2", "operador"], "pausadas": [],
            "red_ndi": [{"nombre": "STUDIO-PC (OBS Program)"}, {"nombre": "VMIX-CONTROL (Output 1)"}],
            "puertos": {"rtmp": 1935, "srt": 8890, "hls": 8888}, "limite_lite": 1, "pro": True,
            "pantallas": [{"conector": "HDMI-A-1", "id": 95, "modo": "1920x1080", "ruta": "operador", "ruta_nombre": n["operador"]},
                          {"conector": "DP-1", "id": 112, "modo": "2560x1440", "ruta": None, "ruta_nombre": None}]}


def estado_rutas(paso, lang="es"):
    """El estado general con el motor de rutas: sin cámaras por modos, con rutas."""
    e = estado(paso, "monitor")
    e.update(fuentes=[], modo="emisor",
             rutas={"total": 3, "en_marcha": 3,
                    "nombres": [NOMBRES[lang][k] for k in ("culto", "cam2", "operador")]},
             capacidad={"edicion": "pro", "potente": True, "nucleos": 4, "memoria_gb": 7.6, "gpu": True,
                        "falta": [], "simultaneo": True, "midiendo": False})
    e["emision"] = {"estado": "emitiendo", "emitiendo": 0, "activas": 0, "receptores": 4}
    e["config"] = {**e["config"], "motor": "rutas", "simultaneo": True}
    e["hdmi"]["mostrando"] = None
    e["eventos"] = [{"t": AHORA - s, "c": c, "d": d} for s, c, d in [
        (20, "rutas_guardadas", {"rutas": 3}), (60, "programa_activa", {"fuente": NOMBRES[lang]["culto"], "salidas": [], "consola": True, "grabar": False}),
        (62, "relevo_activa", {"fuente": NOMBRES[lang]["culto"], "salidas": [], "consola": False, "grabar": False}),
        (70, "emitiendo", {"fuente": "DJI Osmo Pocket 3", "nombre": NOMBRES[lang]["pulpito"], "modo": "1920×1080 @30", "formato": "MJPG", "audio": True}),
        (80, "puente_servidor", {"puerto": 1935}), (90, "motor", {"motor": "rutas"}), (120, "inicio", {"version": VERSION})]]
    return e


REDES = [
    {"ssid": "Studio-5G", "senal": 92, "seguridad": "WPA2", "canal": "149", "banda": "5", "activa": False},
    {"ssid": "Studio", "senal": 84, "seguridad": "WPA2", "canal": "6", "banda": "2.4", "activa": False},
    {"ssid": "Control-Room", "senal": 61, "seguridad": "WPA2 WPA3", "canal": "36", "banda": "5", "activa": False},
    {"ssid": "Guest", "senal": 47, "seguridad": "", "canal": "11", "banda": "2.4", "activa": False},
]


def registro():
    """Líneas de ejemplo del diario de NDIICAM, como las devuelve /api/registro."""
    lineas = [
        (330, "systemd", 6, "Started ndiicam.service - NDIICAM: NDI camera, web panel and setup WiFi."),
        (320, "python3", 6, f'inicio {{"version": "{VERSION}"}}'),
        (318, "python3", 6, 'arranque {"motivo": "reinicio_panel", "detalle": null}'),
        (316, "python3", 6, 'cortafuegos {"detalle": "emisor"}'),
        (312, "python3", 6, 'cam_detectada {"fuente": "Logitech HD Pro Webcam C920"}'),
        (310, "python3", 6, 'cam_detectada {"fuente": "DJI Osmo Pocket 3"}'),
        (304, "python3", 6, 'emitiendo {"fuente": "DJI Osmo Pocket 3", "nombre": "Stage", "modo": "1920×1080 @30", "formato": "MJPG", "audio": true}'),
        (302, "python3", 6, 'gpu_activa {"fuente": "DJI Osmo Pocket 3", "modo": "grabar"}'),
        (300, "python3", 6, 'emitiendo {"fuente": "Logitech HD Pro Webcam C920", "nombre": "Wide", "modo": "1280×720 @30", "formato": "MJPG", "audio": true}'),
        (180, "python3", 4, 'acceso_fallido {"ip": "192.168.1.77"}'),
        (170, "python3", 6, 'acceso {"ip": "192.168.1.20"}'),
        (65, "python3", 6, 'tally {"fuente": "DJI Osmo Pocket 3", "programa": true, "previo": false}'),
        (40, "python3", 6, 'grabacion_manual {"fuente": "DJI Osmo Pocket 3", "grabar": true}'),
    ]
    return {"lineas": [{"t": AHORA - s, "u": u, "p": p, "m": m} for s, u, p, m in lineas]}


def estatico(salida, lang="en", tema="claro", vista="monitor"):
    pestana = {"config": "config", "sistema": "sistema", "rutas": "rutas", "rutas-lista": "rutas"}.get(vista, "monitor")
    estados = [estado(paso, vista, lang) for paso in range(1, 31)]
    portal = vista == "portal"
    respuestas = {
        "/api/sesion": {"autenticado": vista != "entrada", "clave_creada": True, "desde_ap": portal, "espera": 0,
                        "https": not portal, "minimo": 8, "idioma": lang},
        "/api/wifi/redes": {"redes": [dict(r, activa=(r["ssid"] == "Studio-5G" and not portal)) for r in REDES],
                            "guardadas": [] if portal else [
                                {"id": "Studio-5G", "ssid": "Studio-5G", "prioridad": 40, "activa": True},
                                {"id": "Studio", "ssid": "Studio", "prioridad": 20, "activa": False}]},
        "/api/grabaciones": {"lista": [] if vista == "receptor" else [
            {"nombre": "culto_2026-10-25_11-10-00.mkv", "bytes": 1_480_000_000, "fecha": AHORA - 40},
            {"nombre": "culto_2026-10-25_11-00-00.mkv", "bytes": 1_495_000_000, "fecha": AHORA - 640}]
            if vista.startswith("rutas") else [
            {"nombre": "Stage_2026-10-21_10-02-11.mp4", "bytes": 1_412_000_000, "fecha": AHORA - 40},
            {"nombre": "Stage_2026-10-20_18-30-05.mp4", "bytes": 3_850_000_000, "fecha": AHORA - 61000}],
            "libre": 41_200_000_000},
        "/api/registro/arranques": {"arranques": [
            {"id": "", "desde": AHORA - 330, "hasta": AHORA, "actual": True},
            {"id": "b" * 32, "desde": AHORA - 4 * 86400, "hasta": AHORA - 340, "actual": False}]},
        "/api/registro": registro(),
        "/api/rutas": rutas_demo(5, lang),
        "/api/sistema/zonas": {"zonas": ["America/Mexico_City", "America/New_York", "Europe/Lisbon",
                                         "Europe/London", "Europe/Madrid", "UTC"],
                               "zona": "Europe/Madrid", "sincronizada": True},
    }
    previas = {"cam1": preview("escenario"), "cam2": preview("sala"),
               "usb-pocket": preview("escenario"), "usb-mview": preview("multiview")}
    # fetch local: cada consulta del estado recibe el siguiente de la lista
    falso = ("const DEMO_ESTADOS = " + json.dumps(estados) + ";\n"
             "const DEMO_API = " + json.dumps(respuestas) + ";\n"
             "const DEMO_PREVIAS = " + json.dumps(previas) + ";\n"
             "window.fetch = async (ruta) => {\n"
             "  const camino = ruta.split('?')[0];\n"
             "  const d = camino === '/api/estado'\n"
             "    ? DEMO_ESTADOS[Math.min((window.vueltas || 0), DEMO_ESTADOS.length - 1)] : DEMO_API[camino];\n"
             "  const ok = d !== undefined && !(camino === '/api/estado' && !DEMO_API['/api/sesion'].autenticado);\n"
             "  return { ok, status: ok ? 200 : 401, json: async () => JSON.parse(JSON.stringify(d || {})) };\n"
             "};\n"
             f'try {{ localStorage.setItem("ndiicam-lang", "{lang}"); localStorage.setItem("ndiicam-tab", "{pestana}");'
             f' localStorage.removeItem("ndiicam-rt-pos");'
             f' localStorage.setItem("ndiicam-rt-vista", "{"lista" if vista == "rutas-lista" else "lienzo"}"); }} catch (_) {{}}\n')
    html = open(WEB, encoding="utf-8").read()
    html = html.replace('<script>\n"use strict";', '<script>\n"use strict";\n' + falso, 1)
    # 30 consultas rápidas que llenan las gráficas y luego parar: Chrome sin ventana
    # solo hace la foto cuando la página se calma
    html = html.replace("refrescarT = setTimeout(refrescar, 2000);",
                        "if ((window.vueltas = (window.vueltas || 0) + 1) < 30) refrescarT = setTimeout(refrescar, 30);", 1)
    html = html.replace("c.img.src = `/api/fuente/preview?id=${encodeURIComponent(id)}&t=${Date.now()}`;",
                        "c.img.src = DEMO_PREVIAS[id];", 1)
    # las vistas previas de las cámaras de las rutas, también inventadas
    html = html.replace("src: `/api/fuente/preview?id=${encodeURIComponent(n.e.id)}&t=${Date.now()}`",
                        "src: DEMO_PREVIAS[n.e.id] || ''", 1)
    if vista == "rutas":
        # con la ruta del culto abierta en el panel de ajustes
        html = html.replace("rtIniciar();\n</script>", 'rtIniciar();\nsetTimeout(() => rtSeleccionar("r:culto"), 600);\n</script>', 1)
    if tema == "oscuro":
        html = html.replace("@media (prefers-color-scheme: dark)", "@media all", 1)
    if portal:
        html = html.replace("refrescar();\n</script>", 'refrescar();\nsetTimeout(() => $("btBuscar").click(), 300);\n</script>', 1)
    with open(salida, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    estatico(*sys.argv[1:])
