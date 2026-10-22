# NDIICAM — cortafuegos mínimo: solo entra lo que el modo activo necesita.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Una tabla propia de nftables (inet ndiicam) con política de descarte en la
entrada. No toca otras tablas (ufw, Docker...): si el usuario tiene las suyas,
siguen aplicándose. Se rehace cuando cambia el modo o las salidas, y se borra
al parar el servicio o desactivarlo en el panel."""
import re
import subprocess
import time

import nucleo
from gpu import simultaneo
from nucleo import PUERTO_RTMP, PUERTO_SRT, cache_funcion, evento, sh

TABLA = "ndiicam"
# NDI: descubrimiento (5959), mensajes (5960) y un puerto por fuente desde 5961,
# por TCP y por UDP (RUDP). 40 son de sobra para las fuentes de un equipo
PUERTOS_NDI = (5959, 5999)
# Un receptor NDI escucha desde 6960 (TCP y UDP): la fuente abre ahí la conexión de
# vídeo, y si se cierra el receptor ve "conectado" sin imagen. Cada reconexión
# coge el siguiente puerto, así que se deja margen. El multicast va desde 7960 (UDP)
PUERTOS_NDI_RECEPTOR = (6960, 7059)
PUERTOS_NDI_MULTICAST = (7960, 8059)
PUERTO_EXTRA = re.compile(r"^(tcp|udp):(\d{1,5})(?:-(\d{1,5}))?$")


def puertos_extra(texto):
    """'tcp:8080 udp:5000-5010' -> [("tcp", 8080, 8080), ("udp", 5000, 5010)].
    None si algo no vale."""
    res = []
    for parte in re.split(r"[,\s]+", (texto or "").strip().lower()):
        if not parte:
            continue
        m = PUERTO_EXTRA.match(parte)
        if not m:
            return None
        a = int(m.group(2))
        b = int(m.group(3) or a)
        if not 1 <= a <= b <= 65535:
            return None
        res.append((m.group(1), a, b))
    return res


@cache_funcion(300)
def puertos_ssh():
    """Puertos de SSH: nunca se cierran (no dejar al usuario fuera). Los que tiene
    abiertos sshd, los de ssh.socket (Ubuntu 24.04 lo arranca por socket) y los
    de su configuración."""
    puertos = {22}
    for linea in sh(["ss", "-ltnpH"]).splitlines():
        p = linea.split()
        if '"sshd"' in linea and len(p) > 3 and p[3].rsplit(":", 1)[-1].isdigit():
            puertos.add(int(p[3].rsplit(":", 1)[-1]))
    texto = sh(["systemctl", "show", "-p", "Listen", "ssh.socket"])
    texto += sh(["sh", "-c", "cat /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null"])
    puertos |= {int(x) for x in re.findall(r"(?m)^\s*Port\s+(\d+)", texto)}
    puertos |= {int(x) for x in re.findall(r":(\d+) \(Stream\)", texto)}
    return sorted(x for x in puertos if 0 < x < 65536)


def reglas(modo, srt, extra, ssh):
    """Guion de nft. modo: emisor | receptor | puente, o varios a la vez (en un
    equipo potente las cámaras emiten mientras recibe); srt: puertos UDP de las
    salidas SRT de las cámaras; extra: lista de puertos_extra(); ssh: puertos."""
    modos = {modo} if isinstance(modo, str) else set(modo)
    tcp = {*ssh, 80, 443, 53}
    udp = {53, 67, 68, 546, 5353}       # DNS y DHCP de la WiFi de configuración, clientes DHCP, mDNS
    rangos = []
    if modos & {"emisor", "receptor"}:
        rangos += [("tcp", *PUERTOS_NDI), ("udp", *PUERTOS_NDI)]
    if "receptor" in modos:
        rangos += [("tcp", *PUERTOS_NDI_RECEPTOR), ("udp", *PUERTOS_NDI_RECEPTOR),
                   ("udp", *PUERTOS_NDI_MULTICAST)]
    if "emisor" in modos:
        udp |= set(srt)
    if "puente" in modos:
        tcp.add(PUERTO_RTMP)
        udp.add(PUERTO_SRT)
    for proto, a, b in extra:
        if a == b:
            (tcp if proto == "tcp" else udp).add(a)
        else:
            rangos.append((proto, a, b))

    def conjunto(p):
        return ", ".join(str(x) for x in sorted(p))
    lineas = [
        f"table inet {TABLA}", f"delete table inet {TABLA}",
        f"table inet {TABLA} {{",
        "  chain entrada {",
        "    type filter hook input priority filter; policy drop;",
        "    ct state established,related accept",
        "    ct state invalid drop",
        "    iif lo accept",
        "    meta l4proto { icmp, ipv6-icmp } accept",
        f"    tcp dport {{ {conjunto(tcp)} }} accept",
        f"    udp dport {{ {conjunto(udp)} }} accept",
    ]
    lineas += [f"    {p} dport {a}-{b} accept" for p, a, b in rangos]
    lineas += ["  }", "}", ""]
    return "\n".join(lineas)


def aplicar(guion):
    """Carga las reglas de una vez (nft -f es atómico). Devuelve '' o el error."""
    try:
        r = subprocess.run(["nft", "-f", "-"], input=guion, capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError) as ex:
        return str(ex)
    return r.stderr.strip() if r.returncode else ""


def quitar():
    try:
        subprocess.run(["nft", "delete", "table", "inet", TABLA], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


class Cortafuegos:
    def __init__(self):
        self.guion = None
        self.error = None

    def disponible(self):
        return bool(sh(["sh", "-c", "command -v nft"]).strip())

    def actualizar(self, cfg, fuentes):
        """cfg: la configuración; fuentes: ajustes efectivos de cada cámara."""
        if not cfg.get("cortafuegos", True):
            if self.guion is not None:
                quitar()
                self.guion = None
            self.error = None
            return
        varias = simultaneo(cfg)        # salidas adicionales por cámara
        srt = [f["srt_puerto"] for f in fuentes if f.get("activa", True) and (
            f.get("gpu_salida") == "srt"
            or varias and f.get("gpu_salida", "no") != "no" and "srt" in (f.get("gpu_extra") or []))]
        modos = {cfg["modo"]}
        if cfg["modo"] != "emisor" and simultaneo(cfg):
            modos.add("emisor")         # las cámaras siguen emitiendo
        guion = reglas(sorted(modos), srt, puertos_extra(cfg.get("puertos_extra", "")) or [],
                       puertos_ssh())
        if guion == self.guion:
            return
        error = aplicar(guion) or None
        if error != self.error:
            evento("cortafuegos_error" if error else "cortafuegos", detalle=error or " + ".join(sorted(modos)))
        self.error = error
        self.guion = guion if error is None else None

    def estado(self, cfg):
        activo = self.guion is not None
        abiertos = []
        if activo:
            for m in re.finditer(r"(tcp|udp) dport (?:\{ ([^}]*) \}|(\d+-\d+))", self.guion):
                abiertos += [f"{m.group(1)}/{p.strip()}" for p in (m.group(2) or m.group(3)).split(",")]
        return {"activado": bool(cfg.get("cortafuegos", True)), "activo": activo,
                "error": self.error, "abiertos": abiertos}

    def detener(self):
        if self.guion is not None:
            quitar()
            self.guion = None

    def bucle(self):
        """Cada 5 s: el modo y las salidas SRT pueden cambiar desde el panel."""
        if not self.disponible():
            self.error = "sin_nft"
            return
        while True:
            try:
                self.actualizar(nucleo.CONF.get(),
                                [nucleo.CONF.fuente(f.cam["id"]) for f in nucleo.FUENTES.lista()])
            except Exception as ex:
                evento("fallo_interno", detalle=str(ex))
            time.sleep(5)
