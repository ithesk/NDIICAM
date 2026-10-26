# NDIICAM — cámaras USB, pantallas HDMI y dispositivos de audio.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
import glob
import os
import re
import shutil
import subprocess

from nucleo import GRUB_NDIICAM, MODOS_CONSOLA, leer, ruta_usb, sh


# ------------------------------------------------------------------- cámaras

_cache_modos = {}

def modos_captura(dev):
    """Modos (ancho, alto, fps, formato) en MJPG, YUYV y H264 (el que comprime la
    propia cámara), cacheados por nodo."""
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
            if m and fmt in ("MJPG", "YUYV", "H264") and tam:
                modos.add((tam[0], tam[1], round(float(m.group(1))), fmt))
        if not modos:
            # sin modos todavía (una cámara que tarda en arrancar): no se guarda, que
            # si no quedaría invisible hasta que el nodo cambie
            return []
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
