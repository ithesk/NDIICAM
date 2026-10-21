# NDIICAM — el equipo: watchdog, motivo del último reinicio, registro, zona horaria.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
import glob
import json
import os
import re
import subprocess
import time

from nucleo import DATOS, cache_funcion, evento, leer, sh

ARRANQUE = os.path.join(DATOS, "arranque.json")
PSTORE = "/var/lib/systemd/pstore"
WDIOF_CARDRESET = 0x20      # el watchdog reinició el equipo (linux/watchdog.h)
ORIGENES = ("servicio", "sistema", "kernel")
NIVELES = {"todo": 7, "aviso": 4, "error": 3}
ID_ARRANQUE = re.compile(r"^[0-9a-f]{32}$")
MOTIVO = None       # el del arranque actual: lo calcula main()
ZONA = re.compile(r"^[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)*$")


def id_arranque():
    return leer("/proc/sys/kernel/random/boot_id").replace("-", "")


@cache_funcion(30)
def watchdog():
    """Watchdog de la placa: lo alimenta systemd (RuntimeWatchdogSec)."""
    base = "/sys/class/watchdog/watchdog0"
    if not os.path.isdir(base):
        return {"activo": False, "dispositivo": None, "timeout": None}
    t = leer(f"{base}/timeout")
    return {"activo": leer(f"{base}/state") == "active", "dispositivo": leer(f"{base}/identity") or None,
            "timeout": int(t) if t.isdigit() else None}


# ------------------------------------------------------- motivo del reinicio

def marcar_fin(motivo, sobrescribir=True):
    """Apunta cómo termina este arranque: reinicio_panel, apagado_panel,
    reinicio o apagado (del sistema, por SSH o el botón de encendido). Sin
    sobrescribir, respeta lo que ya hubiera apuntado el panel."""
    try:
        with open(ARRANQUE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    if not sobrescribir and d.get("boot") == id_arranque() and d.get("fin"):
        return
    d.update(boot=id_arranque(), fin=motivo, t_fin=time.time())
    try:
        with open(ARRANQUE + ".tmp", "w") as f:
            json.dump(d, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(ARRANQUE + ".tmp", ARRANQUE)
    except OSError:
        pass


def apagado_en_curso():
    """Si el sistema se está apagando o reiniciando: 'reinicio', 'apagado' o None.
    Se mira al recibir SIGTERM para distinguirlo de un simple reinicio del servicio."""
    trabajos = sh(["systemctl", "list-jobs", "--no-legend"], timeout=5)
    if re.search(r"\b(reboot|kexec)\.target", trabajos):
        return "reinicio"
    if re.search(r"\b(poweroff|halt)\.target", trabajos):
        return "apagado"
    if sh(["systemctl", "is-system-running"], timeout=5).strip() == "stopping":
        return "reinicio"     # sin saber cuál: lo importante es que fue ordenado
    return None


def _diario_arranque(boot):
    """Últimas líneas y la hora de la última entrada de un arranque anterior."""
    salida = sh(["journalctl", "-b", boot, "-n", "60", "-o", "json", "--no-pager",
                 "--output-fields=MESSAGE,SYSLOG_IDENTIFIER"], timeout=20)
    lineas, ultima = [], None
    for l in salida.splitlines():
        try:
            e = json.loads(l)
        except ValueError:
            continue
        m = e.get("MESSAGE")
        lineas.append(f"{e.get('SYSLOG_IDENTIFIER', '')}: {m if isinstance(m, str) else ''}")
        ultima = int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6 or ultima
    return lineas, ultima


def apagado_ordenado(lineas):
    """Si el diario de un arranque termina en un apagado del sistema. Ojo: el
    systemd de cada sesión de usuario también llega a su 'shutdown.target' al
    cerrarla (sin 'System'), y eso no es apagar el equipo."""
    for l in lineas:
        if l.startswith("systemd-shutdown:") or re.search(
                r"^systemd: Reached target \S+ - System (Shutdown|Reboot|Power Off|Halt)", l):
            return True
    return False


def volcados():
    """Carpetas de systemd-pstore: una por fallo del kernel, con su hora de nombre."""
    return sorted(os.path.basename(r) for r in glob.glob(f"{PSTORE}/*")
                  if os.path.basename(r).isdigit())


def _panico_pstore(vistos):
    """Un volcado del kernel nuevo, que no estaba en el arranque anterior.
    Devuelve su primera línea útil o None."""
    for nombre in volcados():
        if nombre in vistos:
            continue
        texto = leer(os.path.join(PSTORE, nombre, "001", "dmesg.txt")) or ""
        m = re.search(r"(Kernel panic[^\n]*|sysrq: Trigger a crash|BUG: [^\n]*|Oops[^\n]*)", texto)
        return (m.group(1) if m else "kernel").strip()[:200]
    return None


def calcular_motivo():
    """Por qué arrancó el equipo esta vez, mirando cómo terminó el anterior.
    Se calcula una vez por arranque; los reinicios del servicio lo reutilizan."""
    actual = id_arranque()
    try:
        with open(ARRANQUE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    if d.get("boot") == actual and "motivo" in d:
        return d["motivo"]
    anterior = d.get("boot")
    motivo = {"codigo": "primero", "ultima_senal": None, "detalle": None, "t": time.time()}
    if anterior and ID_ARRANQUE.match(anterior):
        lineas, ultima = _diario_arranque(anterior)
        motivo["ultima_senal"] = ultima or d.get("t_fin")
        texto = "\n".join(lineas)
        wd = leer("/sys/class/watchdog/watchdog0/bootstatus")
        # sin la lista (versión anterior), se toman como vistos los de antes de ese arranque
        vistos = d.get("pstore")
        if vistos is None:
            vistos = [n for n in volcados() if int(n) < d.get("inicio", d.get("t", 0))]
        panico = _panico_pstore(set(vistos))
        if panico or "Kernel panic" in texto:
            motivo.update(codigo="panico", detalle=panico)
        elif d.get("fin") in ("reinicio_panel", "apagado_panel", "reinicio", "apagado"):
            motivo["codigo"] = d["fin"]
        elif apagado_ordenado(lineas):
            motivo["codigo"] = "reinicio"
        elif wd.isdigit() and int(wd) & WDIOF_CARDRESET:
            motivo["codigo"] = "watchdog"
        else:
            # sin rastro de apagado: corte de luz, o un cuelgue que reinició el watchdog
            motivo["codigo"] = "inesperado"
    if motivo["codigo"] != "primero":
        evento("arranque", motivo=motivo["codigo"], detalle=motivo["detalle"])
    uptime = float((leer("/proc/uptime", "0") or "0").split()[0])
    d = {"boot": actual, "t": time.time(), "inicio": time.time() - uptime, "fin": None,
         "motivo": motivo, "pstore": volcados()}
    try:
        with open(ARRANQUE + ".tmp", "w") as f:
            json.dump(d, f)
        os.replace(ARRANQUE + ".tmp", ARRANQUE)
    except OSError:
        pass
    return motivo


# ------------------------------------------------------------------ registro

def arranques(limite=10):
    """Arranques guardados en el diario, del más reciente al más antiguo."""
    try:
        lista = json.loads(sh(["journalctl", "--list-boots", "-o", "json", "--no-pager"],
                              timeout=20) or "[]")
    except ValueError:
        return []
    actual = id_arranque()
    res = [{"id": b["boot_id"], "desde": b["first_entry"] / 1e6, "hasta": b["last_entry"] / 1e6,
            "actual": b["boot_id"] == actual} for b in lista if "boot_id" in b]
    # el orden del diario se lía si el reloj de la placa arranca mal: el actual primero
    res.sort(key=lambda b: (b["actual"], b["hasta"]), reverse=True)
    return res[:limite]


def registro(origen="servicio", arranque="", nivel="todo", buscar="", n=300):
    """Líneas del diario de systemd: [{t, p, u, m}] del más antiguo al más nuevo."""
    if origen not in ORIGENES or nivel not in NIVELES:
        return None
    orden = ["journalctl", "--no-pager", "-o", "json", "-n", str(max(10, min(int(n), 2000))),
             "-p", str(NIVELES[nivel]),
             "--output-fields=MESSAGE,PRIORITY,SYSLOG_IDENTIFIER,_SYSTEMD_UNIT"]
    if arranque:
        if not ID_ARRANQUE.match(arranque):
            return None
        orden += ["-b", arranque]
    else:
        orden += ["-b", "0"] if origen == "kernel" else []
    if origen == "servicio":
        orden += ["-u", "ndiicam.service", "-u", "ndiicam-actualizar.service"]
    elif origen == "kernel":
        orden += ["-k"]
    if buscar:
        orden += ["--grep", re.escape(buscar[:100]), "--case-sensitive=false"]
    res = []
    for linea in sh(orden, timeout=30).splitlines():
        try:
            e = json.loads(linea)
        except ValueError:
            continue
        m = e.get("MESSAGE")
        if isinstance(m, list):     # bytes no UTF-8
            m = bytes(x for x in m if isinstance(x, int)).decode(errors="replace")
        res.append({"t": int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6,
                    "p": int(e.get("PRIORITY", 6)) if str(e.get("PRIORITY", "6")).isdigit() else 6,
                    "u": e.get("SYSLOG_IDENTIFIER") or e.get("_SYSTEMD_UNIT") or "",
                    "m": m or ""})
    return res


def registro_texto(lineas):
    return "".join(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(x["t"]))
                   + f" {x['u']}: {x['m']}\n" for x in lineas)


# --------------------------------------------------------------- zona horaria

@cache_funcion(3600)
def zonas():
    return [z for z in sh(["timedatectl", "list-timezones", "--no-pager"]).split() if ZONA.match(z)]


@cache_funcion(30)
def hora():
    """Zona y si el reloj está sincronizado por NTP."""
    d = dict(l.split("=", 1) for l in sh(["timedatectl", "show"]).splitlines() if "=" in l)
    return {"zona": d.get("Timezone") or "UTC", "sincronizada": d.get("NTPSynchronized") == "yes",
            "ntp": d.get("NTP") == "yes"}


def cambiar_zona(zona):
    if zona not in zonas():
        return False
    try:
        ok = subprocess.run(["timedatectl", "set-timezone", zona], capture_output=True,
                            timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    hora.olvidar()
    # el proceso arrastra la zona con la que arrancó: time.tzset la vuelve a leer
    os.environ.pop("TZ", None)
    time.tzset()
    return ok
