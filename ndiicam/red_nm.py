#!/usr/bin/env python3
# NDIICAM — paso de la red de netplan/systemd-networkd a NetworkManager.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Pasa la red a NetworkManager sin perder el acceso remoto.

  red_nm.py preparar   copia de seguridad, perfiles nuevos, vuelta atrás armada
                       y lanza 'aplicar' fuera de la sesión (SSH puede caerse)
  red_nm.py aplicar    hace el cambio y lo verifica; si no hay red, revierte
  red_nm.py revertir   deja la red exactamente como estaba

Lecciones de la primera migración real:
  - el wpa_supplicant que netplan arranca para networkd (netplan-wpa-<if>)
    sigue agarrado a la tarjeta y NetworkManager no puede usarla: hay que pararlo;
  - networkd y NM se identifican distinto ante el DHCP: se copia a NM el
    client-id de networkd para que el router dé la misma IP;
  - la vuelta atrás se arma ANTES de tocar nada.
"""
import glob
import os
import shutil
import subprocess
import sys
import time

import yaml

RESPALDO = "/var/lib/ndiicam/netplan-respaldo"
RENDERER = "/etc/netplan/01-ndiicam-networkmanager.yaml"
REGISTRO = "/var/lib/ndiicam/red.log"
YO = os.path.abspath(__file__)


def log(texto):
    linea = time.strftime("%Y-%m-%d %H:%M:%S ") + texto
    print(texto, flush=True)
    os.makedirs(os.path.dirname(REGISTRO), exist_ok=True)
    with open(REGISTRO, "a") as f:
        f.write(linea + "\n")


def sh(*cmd, check=False):
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def nm_ya_gestiona():
    """True si NetworkManager ya gestiona alguna interfaz física."""
    if sh("systemctl", "is-active", "NetworkManager").stdout.strip() != "active":
        return False
    for linea in sh("nmcli", "-t", "-f", "TYPE,STATE", "dev").stdout.splitlines():
        tipo, _, estado = linea.partition(":")
        if tipo in ("wifi", "ethernet") and estado not in ("unmanaged", "unavailable"):
            return True
    return False


def client_id(iface):
    """client-id DHCP que usa systemd-networkd en esa interfaz, si hay lease."""
    try:
        idx = open(f"/sys/class/net/{iface}/ifindex").read().strip()
        for linea in open(f"/run/systemd/netif/leases/{idx}"):
            if linea.startswith("CLIENTID="):
                h = linea.split("=", 1)[1].strip()
                return ":".join(h[i:i + 2] for i in range(0, len(h), 2))
    except OSError:
        pass
    return None


def ajustar(passthrough, cid, wifi):
    if cid:
        passthrough["ipv4.dhcp-client-id"] = cid
    if wifi:
        # si la red vuelve tras un rato, reconectar: sin límite de intentos
        passthrough["connection.autoconnect-retries"] = "0"


def preparar():
    if os.geteuid():
        sys.exit("Hace falta root")
    if nm_ya_gestiona():
        log("NetworkManager ya gestiona la red: no hay nada que cambiar")
        return
    ficheros = sorted(glob.glob("/etc/netplan/*.yaml"))
    if not ficheros:
        log("No hay configuración de netplan: configura la red con NetworkManager a mano "
            "(ver docs/network.md)")
        sys.exit(2)
    shutil.rmtree(RESPALDO, ignore_errors=True)
    os.makedirs(RESPALDO)
    for f in ficheros:
        shutil.copy2(f, RESPALDO)
    log(f"Copia de seguridad de netplan en {RESPALDO}")

    for f in ficheros:
        with open(f) as fh:
            d = yaml.safe_load(fh) or {}
        red = d.get("network") or {}
        red.pop("renderer", None)
        for tipo in ("ethernets", "wifis"):
            for iface, defn in (red.get(tipo) or {}).items():
                if not isinstance(defn, dict):
                    continue
                defn.pop("renderer", None)
                cid = client_id(iface)
                if tipo == "wifis":
                    for ap in (defn.get("access-points") or {}).values():
                        if isinstance(ap, dict):
                            pt = ap.setdefault("networkmanager", {}).setdefault("passthrough", {})
                            ajustar(pt, cid, wifi=True)
                elif cid:
                    pt = defn.setdefault("networkmanager", {}).setdefault("passthrough", {})
                    ajustar(pt, cid, wifi=False)
        fd = os.open(f, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(fd, "w") as fh:
            yaml.safe_dump(d, fh, sort_keys=False, allow_unicode=True)
    with open(RENDERER, "w") as fh:
        fh.write("# NDIICAM: la red la gestiona NetworkManager\n"
                 "network:\n  version: 2\n  renderer: NetworkManager\n")
    os.chmod(RENDERER, 0o600)
    r = sh("netplan", "generate")
    if r.returncode:
        log("netplan no acepta la configuración nueva; se deja como estaba:\n" + r.stderr)
        revertir(reiniciar_red=False)
        sys.exit(1)

    # vuelta atrás armada antes del cambio; 'aplicar' la cancela si todo va bien
    sh("systemctl", "reset-failed", "ndiicam-red-revertir.timer",
       "ndiicam-red-revertir.service", "ndiicam-red-aplicar.service")
    sh("systemd-run", "--unit=ndiicam-red-revertir", "--on-active=300",
       sys.executable, YO, "revertir", check=True)
    sh("systemd-run", "--unit=ndiicam-red-aplicar", sys.executable, YO, "aplicar",
       check=True)
    log("Cambio lanzado: la red se cortará unos segundos. Si en 90 s no hay conexión, "
        "se revierte sola.")


def conectado():
    estado = sh("nmcli", "-t", "-f", "STATE", "general").stdout.strip()
    if estado not in ("connected", "connected (site only)", "connected (local only)"):
        return False
    ruta = sh("ip", "route", "show", "default").stdout.split()
    return "via" in ruta and sh("ping", "-c1", "-W2", ruta[ruta.index("via") + 1]).returncode == 0


def aplicar():
    time.sleep(2)
    sh("netplan", "apply")
    for linea in sh("systemctl", "list-units", "--plain", "--no-legend",
                    "netplan-wpa-*.service").stdout.splitlines():
        sh("systemctl", "stop", linea.split()[0])
    sh("systemctl", "stop", "systemd-networkd.socket", "systemd-networkd.service")
    sh("systemctl", "restart", "wpa_supplicant")
    sh("systemctl", "restart", "NetworkManager")
    for _ in range(45):
        time.sleep(2)
        if conectado():
            sh("systemctl", "stop", "ndiicam-red-revertir.timer")
            # networkd ya no hace falta, y su wait-online retrasaría el arranque
            sh("systemctl", "disable", "systemd-networkd.service",
               "systemd-networkd.socket", "systemd-networkd-wait-online.service")
            log("Red pasada a NetworkManager y verificada")
            return
    log("Sin conexión con NetworkManager tras 90 s: revirtiendo")
    revertir()


def revertir(reiniciar_red=True):
    if os.path.exists(RENDERER):
        os.remove(RENDERER)
    for f in glob.glob(f"{RESPALDO}/*.yaml"):
        shutil.copy2(f, "/etc/netplan/")
    if reiniciar_red:
        sh("systemctl", "stop", "NetworkManager")
        sh("netplan", "apply")
        sh("systemctl", "restart", "systemd-networkd")
        log("Red restaurada como estaba (systemd-networkd)")


if __name__ == "__main__":
    ordenes = {"preparar": preparar, "aplicar": aplicar, "revertir": revertir}
    if len(sys.argv) != 2 or sys.argv[1] not in ordenes:
        sys.exit(__doc__)
    ordenes[sys.argv[1]]()
