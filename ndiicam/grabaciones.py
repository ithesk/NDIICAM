# NDIICAM — grabaciones por sesión: carpetas, trozos, marcas, disco, unión y USB.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Una grabación es una **sesión**: una carpeta por cada vez que una ruta empieza a
grabar, con sus trozos de diez minutos y un `sesion.json` que dice de qué ruta es,
cuándo empezó y acabó, dónde van las marcas y si está protegida.

    GRABACIONES/
      Culto_2026-10-25_11-00-00/
        sesion.json
        trozo-01.mkv  trozo-02.mkv ...
        completa.mkv      (los trozos unidos, cuando se pide; sin recodificar)
        miniatura.jpg
      nueva-ruta-1_2026-10-22_23-14-36.mp4   (ficheros sueltos de antes: siguen valiendo)

Los procesos que graban (relevo.py, programa.py) abren la sesión y piden aquí cada
trozo; el panel lista, protege, marca, une, borra y copia a un USB. Todo lo que
escribe el json es atómico (tmp + replace): un corte de luz no lo deja a medias.
Lo que tarda (unir, miniatura, copiar) va en procesos o hilos aparte y aquí solo
se consulta su estado."""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from nucleo import GRABACIONES

DIR = GRABACIONES
SESION_RE = re.compile(r"^[\w.-]{1,120}$")
TROZO_RE = re.compile(r"^trozo-(\d{2,4})\.(mp4|mkv)$")
SUELTO_RE = re.compile(r"^[\w.-]{1,120}\.(mp4|mkv)$")
RESERVA_MINIMA = 2 * 1024 ** 3      # lo que tiene que quedar libre, como poco
RESERVA_SIN_TASA = 4 * 1024 ** 3    # si no se sabe a cuánto se graba (una copia del ATEM)
SEGMENTO_S = 10 * 60                # duración de cada trozo
AVISO_DISCO = 20 * 1024 ** 3        # por debajo de esto, el panel avisa
MONTAJES = "/media/ndiicam"
UNIR_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unir.py")

_lock = threading.Lock()
_uniendo = {}       # sesión -> Popen del unir.py en marcha
_miniaturas = {}    # sesión -> Popen
_copia = {"en_curso": False, "sesion": None, "hechas": 0, "total_bytes": 0, "bytes": 0, "error": None}
_copia_hilo = None


# ------------------------------------------------------------------ utilidades

def _ruta(sesion):
    return os.path.join(DIR, sesion)


def _json(sesion):
    return os.path.join(_ruta(sesion), "sesion.json")


def _leer(sesion):
    try:
        with open(_json(sesion)) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _escribir(sesion, datos):
    ruta = _json(sesion)
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    tmp = ruta + ".tmp"
    with open(tmp, "w") as f:
        json.dump(datos, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, ruta)


def _tamano(ruta):
    try:
        return os.path.getsize(ruta)
    except OSError:
        return 0


def _hora(segundos):
    s = max(0, int(segundos))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def slug(nombre):
    """El nombre de una ruta como trozo de nombre de fichero: letras, números y
    guiones; "Culto " → "Culto"; vacío → "ruta"."""
    s = re.sub(r"[^\w-]+", "-", str(nombre or "").strip(), flags=re.UNICODE).strip("-_")
    s = re.sub(r"-{2,}", "-", s)
    return s[:60] or "ruta"


def nombre_sesion(nombre_ruta, inicio_ts):
    return f"{slug(nombre_ruta)}_{time.strftime('%Y-%m-%d_%H-%M-%S', time.localtime(inicio_ts))}"


def reserva_disco(reserva=None, kbps=None):
    """Lo que tiene que quedar libre: dos trozos a la tasa que se graba y nunca
    menos de 2 GB; sin tasa conocida, 4 GB."""
    if reserva is not None:
        return int(reserva)
    if kbps:
        return max(RESERVA_MINIMA, 2 * SEGMENTO_S * int(kbps) * 1000 // 8)
    return RESERVA_SIN_TASA


# ------------------------------------------------------------- sesión y trozos

def abrir_sesion(nombre_ruta, ruta_id, formato, inicio_ts=None):
    """Crea la carpeta de una sesión nueva y devuelve su nombre. Si ya existe una
    con ese nombre (dos arranques en el mismo segundo), se numera."""
    inicio = time.time() if inicio_ts is None else inicio_ts
    base = nombre_sesion(nombre_ruta, inicio)
    with _lock:
        nombre, n = base, 2
        while os.path.exists(_ruta(nombre)):
            nombre = f"{base}-{n}"
            n += 1
        _escribir(nombre, {"nombre": nombre, "ruta": ruta_id, "ruta_nombre": str(nombre_ruta or ""),
                           "formato": "mkv" if formato == "mkv" else "mp4", "inicio": inicio, "fin": None,
                           "trozos": [], "marcas": [], "protegida": False})
    return nombre


def _cerrar_trozo(d, sesion, fin):
    if d["trozos"] and d["trozos"][-1].get("fin") is None:
        t = d["trozos"][-1]
        t["fin"] = fin
        t["bytes"] = _tamano(os.path.join(_ruta(sesion), t["fichero"]))


def nuevo_trozo(sesion, inicio_ts=None):
    """El fichero del siguiente trozo (trozo-NN.ext), ya apuntado en la sesión. El
    trozo anterior, si quedó abierto, se da por cerrado en este instante. La
    `completa` de antes ya no vale: se borra."""
    inicio = time.time() if inicio_ts is None else inicio_ts
    with _lock:
        d = _leer(sesion)
        if d is None:
            raise FileNotFoundError(sesion)
        _cerrar_trozo(d, sesion, inicio)
        n = len(d["trozos"]) + 1
        fichero = f"trozo-{n:02d}.{d['formato']}"
        d["trozos"].append({"fichero": fichero, "inicio": inicio, "fin": None, "bytes": None})
        _escribir(sesion, d)
        for c in glob.glob(os.path.join(_ruta(sesion), "completa.*")):
            try:
                os.remove(c)
            except OSError:
                pass
    return os.path.join(_ruta(sesion), fichero)


def cerrar_sesion(sesion, fin_ts=None):
    fin = time.time() if fin_ts is None else fin_ts
    with _lock:
        d = _leer(sesion)
        if d is None:
            return
        _cerrar_trozo(d, sesion, fin)
        d["fin"] = fin
        _escribir(sesion, d)


def borrar(nombre):
    if not SESION_RE.match(nombre):
        return False
    with _lock:
        ruta = _ruta(nombre)
        if os.path.isdir(ruta):
            shutil.rmtree(ruta, ignore_errors=True)
            return True
        if SUELTO_RE.match(nombre) and os.path.isfile(ruta):
            os.remove(ruta)
            return True
    return False


def proteger(nombre, si):
    with _lock:
        d = _leer(nombre)
        if d is None:
            return False
        d["protegida"] = bool(si)
        _escribir(nombre, d)
    return True


# ------------------------------------------------------------------- marcas

def marca(nombre, texto="", t=None):
    """Una marca en la sesión: t en segundos desde el inicio (None: ahora)."""
    with _lock:
        d = _leer(nombre)
        if d is None:
            return None
        if t is None:
            t = time.time() - d["inicio"]
        t = max(0.0, float(t))
        m = {"t": round(t, 1), "hora": _hora(t), "texto": str(texto or "")[:120]}
        d["marcas"].append(m)
        d["marcas"].sort(key=lambda x: x["t"])
        _escribir(nombre, d)
    return m


def marcas_txt(nombre):
    """Las marcas como capítulos de YouTube: una por línea, con 00:00:00 delante."""
    d = _leer(nombre)
    if d is None:
        return ""
    lineas = []
    if not any(int(m["t"]) == 0 for m in d["marcas"]):
        lineas.append("00:00:00 Inicio")
    for m in d["marcas"]:
        lineas.append(f"{_hora(m['t'])} {m['texto'] or 'Marca'}")
    return "\n".join(lineas) + "\n"


# --------------------------------------------------------------------- listar

def _estado_union(sesion, d):
    ruta = _ruta(sesion)
    fichero = f"completa.{d['formato']}"
    completa = os.path.join(ruta, fichero)
    proc = _uniendo.get(sesion)
    if proc is not None and proc.poll() is None:
        return {"estado": "preparando", "fichero": None, "bytes": _tamano(completa + ".tmp")}
    if os.path.exists(completa):
        return {"estado": "lista", "fichero": fichero, "bytes": _tamano(completa)}
    if proc is not None and proc.poll() not in (None, 0):
        return {"estado": "error", "fichero": None, "bytes": 0}
    if os.path.exists(completa + ".tmp"):
        return {"estado": "preparando", "fichero": None, "bytes": _tamano(completa + ".tmp")}
    return {"estado": "no", "fichero": None, "bytes": 0}


def _sesion(nombre, vivas, ahora):
    d = _leer(nombre)
    if d is None:
        return None
    ruta = _ruta(nombre)
    viva = nombre in vivas
    trozos, total, duracion = [], 0, 0.0
    for i, t in enumerate(d.get("trozos") or []):
        abierto = t.get("fin") is None and i == len(d["trozos"]) - 1
        fin = t.get("fin") if t.get("fin") is not None else (ahora if viva else None)
        dur = max(0.0, (fin - t["inicio"])) if fin is not None else 0.0
        b = t.get("bytes") if t.get("bytes") is not None else _tamano(os.path.join(ruta, t["fichero"]))
        total += b
        duracion += dur
        trozos.append({"fichero": t["fichero"], "inicio": t["inicio"], "fin": t.get("fin"), "duracion": round(dur, 1),
                       "bytes": b, "grabando": abierto and viva})
    return {"nombre": nombre, "ruta": d.get("ruta"), "ruta_nombre": d.get("ruta_nombre", ""),
            "formato": d.get("formato", "mp4"), "inicio": d.get("inicio"), "fin": d.get("fin"),
            "duracion": round(duracion, 1), "bytes": total, "trozos": trozos, "marcas": d.get("marcas") or [],
            "protegida": bool(d.get("protegida")), "cortada": d.get("fin") is None and not viva,
            "grabando": viva, "completa": _estado_union(nombre, d),
            "miniatura": os.path.exists(os.path.join(ruta, "miniatura.jpg"))}


def listar(vivas=()):
    """Sesiones (de más nueva a más vieja), ficheros sueltos y el estado del disco.
    vivas: nombres de las sesiones que un proceso está grabando ahora."""
    vivas = set(vivas or ())
    ahora = time.time()
    sesiones, sueltos = [], []
    try:
        nombres = os.listdir(DIR)
    except OSError:
        nombres = []
    for n in nombres:
        ruta = os.path.join(DIR, n)
        if os.path.isdir(ruta) and SESION_RE.match(n) and os.path.exists(os.path.join(ruta, "sesion.json")):
            s = _sesion(n, vivas, ahora)
            if s:
                sesiones.append(s)
        elif os.path.isfile(ruta) and SUELTO_RE.match(n):
            try:
                st = os.stat(ruta)
            except OSError:
                continue
            sueltos.append({"nombre": n, "bytes": st.st_size, "fecha": st.st_mtime})
    sesiones.sort(key=lambda s: s["inicio"] or 0, reverse=True)
    sueltos.sort(key=lambda x: x["fecha"], reverse=True)
    try:
        libre = shutil.disk_usage(DIR).free
    except OSError:
        libre = None
    reserva = reserva_disco()
    return {"sesiones": sesiones, "sueltos": sueltos, "libre": libre, "reserva": reserva,
            "aviso_disco": libre is not None and (libre < 4 * reserva or libre < AVISO_DISCO)}


# ---------------------------------------------------------------------- disco

def liberar_disco(reserva=None, kbps=None, vivas=()):
    """Como un grabador continuo: si queda menos de la reserva, fuera lo más viejo.
    Candidatos: los trozos de sesiones no protegidas (nunca el abierto de una que
    está grabando) y los ficheros sueltos, por fecha. Una sesión que se queda sin
    trozos desaparece. Devuelve los ficheros borrados."""
    reserva = reserva_disco(reserva, kbps)
    vivas = set(vivas or ())
    os.makedirs(DIR, exist_ok=True)
    borrados = []
    with _lock:
        candidatos = []     # (mtime, ruta, sesión | None)
        try:
            nombres = os.listdir(DIR)
        except OSError:
            return borrados
        for n in nombres:
            ruta = os.path.join(DIR, n)
            if os.path.isdir(ruta) and SESION_RE.match(n):
                d = _leer(n)
                if d is None or d.get("protegida"):
                    continue
                trozos = d.get("trozos") or []
                for i, t in enumerate(trozos):
                    abierto = t.get("fin") is None and i == len(trozos) - 1
                    if abierto and n in vivas:
                        continue        # el trozo que se está escribiendo ahora
                    f = os.path.join(ruta, t["fichero"])
                    if os.path.exists(f):
                        candidatos.append((os.path.getmtime(f), f, n))
            elif os.path.isfile(ruta) and SUELTO_RE.match(n):
                candidatos.append((os.path.getmtime(ruta), ruta, None))
        candidatos.sort()
        for _, f, sesion in candidatos:
            if shutil.disk_usage(DIR).free >= reserva:
                break
            try:
                os.remove(f)
                borrados.append(f)
            except OSError:
                continue
            if sesion:
                d = _leer(sesion)
                if d is not None:
                    d["trozos"] = [t for t in d["trozos"] if t["fichero"] != os.path.basename(f)]
                    if d["trozos"]:
                        _escribir(sesion, d)
                    else:
                        shutil.rmtree(_ruta(sesion), ignore_errors=True)
                for c in glob.glob(os.path.join(_ruta(sesion), "completa.*")):
                    try:
                        os.remove(c)
                    except OSError:
                        pass
    return borrados


# ------------------------------------------------------------ unir y miniatura

def _trozos_mtime(sesion, d):
    return max((os.path.getmtime(os.path.join(_ruta(sesion), t["fichero"]))
                for t in d.get("trozos") or [] if os.path.exists(os.path.join(_ruta(sesion), t["fichero"]))),
               default=0)


def unir(nombre):
    """Lanza en segundo plano la unión de los trozos en completa.<ext> (copia sin
    recodificar). Si ya está hecha y los trozos no cambiaron, no hace nada."""
    d = _leer(nombre)
    if d is None or not d.get("trozos"):
        return False
    ruta, ext = _ruta(nombre), d["formato"]
    completa = os.path.join(ruta, f"completa.{ext}")
    proc = _uniendo.get(nombre)
    if proc is not None and proc.poll() is None:
        return True
    if os.path.exists(completa) and os.path.getmtime(completa) >= _trozos_mtime(nombre, d):
        return True
    try:
        os.remove(completa + ".tmp")
    except OSError:
        pass
    _uniendo[nombre] = subprocess.Popen([sys.executable, UNIR_PY, "unir", ruta, ext],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def estado_union(nombre):
    d = _leer(nombre)
    return _estado_union(nombre, d) if d else {"estado": "no", "fichero": None, "bytes": 0}


def miniatura(nombre):
    """Genera miniatura.jpg del primer trozo, en segundo plano (vale sobre un
    trozo que aún se está escribiendo)."""
    d = _leer(nombre)
    if d is None or not d.get("trozos"):
        return False
    ruta = _ruta(nombre)
    primero = os.path.join(ruta, d["trozos"][0]["fichero"])
    if not os.path.exists(primero):
        return False
    proc = _miniaturas.get(nombre)
    if proc is not None and proc.poll() is None:
        return True
    _miniaturas[nombre] = subprocess.Popen([sys.executable, UNIR_PY, "miniatura", primero,
                                            os.path.join(ruta, "miniatura.jpg")],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


# ------------------------------------------------------------------------ USB

def _lsblk():
    try:
        r = subprocess.run(["lsblk", "-J", "-b", "-o", "NAME,PATH,TRAN,RM,SIZE,FSTYPE,LABEL,MOUNTPOINT,TYPE"],
                           capture_output=True, text=True, timeout=10)
        return json.loads(r.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"blockdevices": []}


def usb_discos(salida=None):
    """Particiones con sistema de ficheros de discos USB o extraíbles."""
    datos = salida if salida is not None else _lsblk()
    res = []

    def recorrer(dev, usb):
        es_usb = usb or dev.get("tran") == "usb" or str(dev.get("rm")) in ("1", "True", "true")
        if dev.get("type") == "part" and es_usb and dev.get("fstype"):
            montado = dev.get("mountpoint") or None
            libre = None
            if montado:
                try:
                    libre = shutil.disk_usage(montado).free
                except OSError:
                    pass
            res.append({"dev": dev.get("path") or "/dev/" + dev["name"], "etiqueta": dev.get("label") or "",
                        "tamano": int(dev.get("size") or 0), "fs": dev.get("fstype"), "montado": montado, "libre": libre})
        for h in dev.get("children") or []:
            recorrer(h, es_usb)
    for dev in datos.get("blockdevices") or []:
        recorrer(dev, False)
    return res


def _disco(dev):
    d = next((x for x in usb_discos() if x["dev"] == dev), None)
    if d is None:
        raise ValueError("usb_no_esta")
    return d


def usb_montar(dev):
    """Monta la partición en /media/ndiicam/<etiqueta o nombre> y devuelve la ruta."""
    d = _disco(dev)
    if d["montado"]:
        return d["montado"]
    nombre = re.sub(r"[^\w-]+", "_", d["etiqueta"] or os.path.basename(dev)).strip("_") or "usb"
    ruta = os.path.join(MONTAJES, nombre)
    os.makedirs(ruta, exist_ok=True)
    orden = ["mount"]
    if d["fs"] in ("vfat", "exfat"):
        orden += ["-o", "uid=0,gid=0,umask=000"]
    elif d["fs"] == "ntfs" and shutil.which("ntfs-3g"):
        orden = ["ntfs-3g", "-o", "umask=000"]
    orden += [dev, ruta]
    r = subprocess.run(orden, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise OSError(r.stderr.strip() or "mount")
    return ruta


def usb_expulsar(dev):
    d = _disco(dev)
    if d["montado"]:
        subprocess.run(["sync"], timeout=120)
        r = subprocess.run(["umount", d["montado"]], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise OSError(r.stderr.strip() or "umount")
    return True


def estado_copia():
    return dict(_copia)


def _copiar(destino, sesiones, unidas):
    try:
        total = 0
        planes = []
        for s in sesiones:
            d = _leer(s)
            if d is None:
                continue
            ruta = _ruta(s)
            ficheros = ["sesion.json"]
            completa = f"completa.{d['formato']}"
            if unidas and os.path.exists(os.path.join(ruta, completa)):
                ficheros.append(completa)
            else:
                ficheros += [t["fichero"] for t in d.get("trozos") or [] if os.path.exists(os.path.join(ruta, t["fichero"]))]
            if os.path.exists(os.path.join(ruta, "miniatura.jpg")):
                ficheros.append("miniatura.jpg")
            total += sum(_tamano(os.path.join(ruta, f)) for f in ficheros)
            planes.append((s, ruta, ficheros))
        _copia.update(total_bytes=total, bytes=0, hechas=0)
        for s, ruta, ficheros in planes:
            _copia["sesion"] = s
            carpeta = os.path.join(destino, "NDIICAM", s)
            os.makedirs(carpeta, exist_ok=True)
            for f in ficheros:
                origen, dest = os.path.join(ruta, f), os.path.join(carpeta, f)
                with open(origen, "rb") as a, open(dest + ".parte", "wb") as b:
                    while True:
                        trozo = a.read(4 * 1024 * 1024)
                        if not trozo:
                            break
                        b.write(trozo)
                        _copia["bytes"] += len(trozo)
                os.replace(dest + ".parte", dest)
            with open(os.path.join(carpeta, "marcas.txt"), "w") as f:
                f.write(marcas_txt(s))
            _copia["hechas"] += 1
        subprocess.run(["sync"], timeout=300)
    except Exception as ex:  # el error se enseña en el panel
        _copia["error"] = str(ex)
    finally:
        _copia["en_curso"] = False
        _copia["sesion"] = None


def copiar_a_usb(dev, sesiones, unidas=False):
    """Copia las sesiones (sus trozos y su json, o la completa si se pide y existe)
    a <USB>/NDIICAM/<sesion>/, en un hilo; el progreso, en estado_copia()."""
    global _copia_hilo
    if _copia["en_curso"]:
        raise RuntimeError("copia_en_curso")
    sesiones = [s for s in sesiones if SESION_RE.match(s) and os.path.isdir(_ruta(s))]
    if not sesiones:
        raise ValueError("sin_sesiones")
    destino = usb_montar(dev)
    _copia.update(en_curso=True, sesion=None, hechas=0, total_bytes=0, bytes=0, error=None)
    _copia_hilo = threading.Thread(target=_copiar, args=(destino, sesiones, unidas), daemon=True)
    _copia_hilo.start()
    return True
