# NDIICAM — actualizaciones desde GitHub, copias de seguridad, fábrica y nombre del equipo.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
import datetime
import json
import os
import re
import shlex
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

import nucleo
from nucleo import (DATOS, NOMBRE_EQUIPO, POR_DEFECTO, REPO, VERSION, configurar_ndi, evento,
    leer, sh, version_tupla)


def reiniciar_servicio(espera=1.5):
    """Sale para que systemd vuelva a arrancar el servicio (Restart=always)."""
    def salir():
        nucleo.RED.detener_ap()
        nucleo.FUENTES.detener_todas()
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
            "equipo": socket.gethostname(), "config": nucleo.CONF.get(),
            "wifi": nucleo.RED.guardadas(con_claves=True) if nucleo.RED.wifi else []}

def restaurar(d):
    """Aplica una copia: ajustes, redes WiFi que falten y nombre del equipo."""
    cfg = d.get("config") or {}
    cambios = {k: cfg[k] for k in POR_DEFECTO if k in cfg}
    if not isinstance(cambios.get("fuentes", {}), dict):
        cambios.pop("fuentes", None)
    if cambios:
        nucleo.CONF.actualizar(cambios)
    anadidas = 0
    if nucleo.RED.wifi:
        actuales = {p["ssid"] for p in nucleo.RED.guardadas()}
        for w in d.get("wifi") or []:
            ssid = str(w.get("ssid", ""))
            if not ssid or ssid in actuales:
                continue
            r = nucleo.RED.anadir_perfil(ssid, str(w.get("clave", "")), w.get("seguridad") or None,
                                  int(w.get("prioridad", 0)), w.get("ip"))
            anadidas += r.returncode == 0
    equipo = str(d.get("equipo", ""))
    if equipo and NOMBRE_EQUIPO.match(equipo) and equipo != socket.gethostname():
        cambiar_nombre_equipo(equipo)
    evento("respaldo_restaurado", fecha=d.get("fecha", ""), wifi=anadidas)

def valores_fabrica():
    nucleo.CONF.restablecer()
    configurar_ndi(POR_DEFECTO["transporte"])
    if nucleo.RED.wifi:
        nucleo.RED.olvidar_todas()
    evento("fabrica")
