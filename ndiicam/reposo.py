#!/usr/bin/env python3
# NDIICAM — pantalla de reposo: lo que se ve por HDMI cuando no hay nada que mostrar.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Dibuja una pantalla de ayuda (estado, red, QR al panel) y la deja fija en el
monitor. Sustituye a la consola de texto del sistema.

    reposo.py '<json>'   con hdmi (id del conector DRM), pantalla ([ancho, alto]),
                         idioma (es | en), equipo, version, estado, modo, ip, red,
                         url, ap ({ssid, clave} o null) y detalle (texto o null)

La imagen se genera con Pillow, el QR con segno, y se muestra con kmssink en
pausa: una vez en pantalla no gasta CPU. El servicio lo relanza cuando cambia
algo (IP, estado, modo) y lo para cuando una cámara o el receptor necesitan
el HDMI.
"""
import io
import json
import os
import signal
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

RUTA_PNG = "/run/ndiicam/reposo.png"
FUENTES = "/usr/share/fonts/truetype/dejavu"
FONDO, TEXTO, SUAVE, ACENTO = (16, 20, 24), (240, 242, 245), (150, 158, 168), (64, 140, 255)
AVISO = (255, 184, 64)

T = {
    "es": {
        "sin_camara": "Conecta una cámara USB", "desactivada": "Sin emisión",
        "iniciando": "Arrancando la emisión…", "emitiendo": "Emitiendo por NDI",
        "sin_fuente": "Elige una fuente NDI en el panel", "sin_senal": "Sin señal",
        "esperando_atem": "Esperando al ATEM", "sin_red": "Sin red",
        "modo": {"emisor": "Modo emisor", "receptor": "Modo receptor", "puente": "Modo puente"},
        "panel": "Panel de control", "escanea": "Escanea para abrir el panel",
        "red": "Red", "cable": "Cable", "wifi": "WiFi", "ip": "IP",
        "ap": "WiFi de configuración", "linea_clave": "Clave: {}", "sin_ip": "sin dirección",
        "consola": "Consola de texto: Alt+F2",
    },
    "en": {
        "sin_camara": "Plug in a USB camera", "desactivada": "Not streaming",
        "iniciando": "Starting the stream…", "emitiendo": "Streaming over NDI",
        "sin_fuente": "Pick an NDI source in the panel", "sin_senal": "No signal",
        "esperando_atem": "Waiting for the ATEM", "sin_red": "No network",
        "modo": {"emisor": "Sender mode", "receptor": "Receiver mode", "puente": "Bridge mode"},
        "panel": "Control panel", "escanea": "Scan to open the panel",
        "red": "Network", "cable": "Wired", "wifi": "WiFi", "ip": "IP",
        "ap": "Setup WiFi", "linea_clave": "Password: {}", "sin_ip": "no address",
        "consola": "Text console: Alt+F2",
    },
}


def dibujar(cfg):
    from PIL import Image, ImageDraw, ImageFont
    import segno

    t = T.get(cfg.get("idioma"), T["es"])
    ancho, alto = cfg.get("pantalla") or [1920, 1080]
    e = alto / 1080                       # escala: todo se diseña para 1080p
    img = Image.new("RGB", (ancho, alto), FONDO)
    d = ImageDraw.Draw(img)

    def fuente(tam, negrita=False):
        nombre = "DejaVuSans-Bold.ttf" if negrita else "DejaVuSans.ttf"
        return ImageFont.truetype(os.path.join(FUENTES, nombre), int(tam * e))

    # --- cabecera
    mx, y = int(110 * e), int(90 * e)
    d.text((mx, y), "NDIICAM", font=fuente(34, True), fill=ACENTO)
    d.text((mx + int(190 * e), y + int(4 * e)), cfg.get("equipo", ""), font=fuente(30), fill=SUAVE)

    # --- estado, grande
    estado = cfg.get("estado", "desactivada")
    color = {"emitiendo": ACENTO, "sin_senal": AVISO, "sin_red": AVISO}.get(estado, TEXTO)
    y = int(300 * e)
    d.text((mx, y), t.get(estado, estado), font=fuente(78, True), fill=color)
    y += int(110 * e)
    if cfg.get("detalle"):
        d.text((mx, y), str(cfg["detalle"])[:90], font=fuente(34), fill=SUAVE)
        y += int(60 * e)
    d.text((mx, y), t["modo"].get(cfg.get("modo", ""), ""), font=fuente(30), fill=SUAVE)

    # --- red y panel
    y = int(640 * e)
    ap = cfg.get("ap")
    if ap:
        d.text((mx, y), f"{t['ap']}: {ap['ssid']}", font=fuente(38, True), fill=AVISO)
        y += int(56 * e)
        d.text((mx, y), t["linea_clave"].format(ap["clave"]), font=fuente(34), fill=TEXTO)
        y += int(70 * e)
    red = cfg.get("red") or {}
    if red.get("tipo") == "wifi":
        linea = f"{t['wifi']}: {red.get('ssid', '')}"
    elif red.get("tipo") == "cable":
        linea = t["cable"]
    else:
        linea = t["sin_red"]
    d.text((mx, y), f"{t['red']}: {linea}", font=fuente(34), fill=TEXTO)
    y += int(52 * e)
    d.text((mx, y), f"{t['ip']}: {cfg.get('ip') or t['sin_ip']}", font=fuente(34), fill=TEXTO)
    y += int(70 * e)
    d.text((mx, y), t["panel"], font=fuente(28), fill=SUAVE)
    y += int(40 * e)
    d.text((mx, y), cfg.get("url", ""), font=fuente(44, True), fill=ACENTO)

    # --- QR a la derecha
    if cfg.get("url"):
        lado = int(440 * e)
        buf = io.BytesIO()
        segno.make(cfg["url"], error="m").save(buf, kind="png", scale=10, border=2,
                                                dark="#000000", light="#ffffff")
        qr = Image.open(buf).convert("RGB").resize((lado, lado), Image.NEAREST)
        x0, y0 = ancho - mx - lado, int(300 * e)
        d.rounded_rectangle((x0 - int(24 * e), y0 - int(24 * e), x0 + lado + int(24 * e),
                             y0 + lado + int(24 * e)), radius=int(24 * e), fill=(255, 255, 255))
        img.paste(qr, (x0, y0))
        texto = t["escanea"]
        w = d.textlength(texto, font=fuente(28))
        d.text((x0 + (lado - w) / 2, y0 + lado + int(44 * e)), texto, font=fuente(28), fill=SUAVE)

    # --- pie
    pie = f"NDIICAM {cfg.get('version', '')}  ·  {t['consola']}"
    d.text((mx, alto - int(70 * e)), pie, font=fuente(22), fill=SUAVE)
    os.makedirs(os.path.dirname(RUTA_PNG), exist_ok=True)
    img.save(RUTA_PNG + ".tmp", "PNG")
    os.replace(RUTA_PNG + ".tmp", RUTA_PNG)


def main():
    # la configuración llega por la entrada estándar (lleva la clave de la WiFi de
    # configuración: que no se vea en la lista de procesos); a mano, como argumento
    cfg = json.loads(sys.argv[1] if len(sys.argv) > 1 else sys.stdin.readline())
    dibujar(cfg)
    Gst.init(None)
    # en pausa: kmssink enseña el fotograma de preparación y no vuelve a trabajar.
    # El plano de vídeo escala si la imagen no es del tamaño de la pantalla
    p = Gst.parse_launch(
        f"filesrc location={RUTA_PNG} ! pngdec ! imagefreeze ! videoconvert"
        " ! video/x-raw,format=BGRx ! kmssink name=hdmi sync=false"
        f" connector-id={cfg['hdmi']}")
    bucle = GLib.MainLoop()

    def mensaje(bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            sys.stdout.write(json.dumps({"error": err.message}) + "\n")
            sys.stdout.flush()
            bucle.quit()

    bus = p.get_bus()
    bus.add_signal_watch()
    bus.connect("message", mensaje)
    for s in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, s, lambda *_: bucle.quit())
    if p.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
        sys.stdout.write(json.dumps({"error": "arranque"}) + "\n")
        sys.exit(1)
    bucle.run()
    p.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
