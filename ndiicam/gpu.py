# NDIICAM — GPU Intel: qué sabe hacer (VA-API, HuC) y cuánto trabaja; energía RAPL y CPU.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
import glob
import os
import re
import subprocess

from nucleo import leer


# ------------------------------------------------------------------- fuentes

_GPU = {}
_DEC_H264 = []

def entorno_va():
    """Entorno para GStreamer VA: con el driver moderno de Intel si está (el
    antiguo, i965, no entrega JPEG 4:2:0 decodificado)."""
    env = dict(os.environ)
    if glob.glob("/usr/lib/*/dri/iHD_drv_video.so"):
        env["LIBVA_DRIVER_NAME"] = "iHD"
    return env

def huc_activo():
    """Firmware HuC de la GPU Intel cargado. Sin él, el codificador de bajo
    consumo (VDEnc) de las Intel Gen9 puede colgar el equipo entero."""
    return any("status: RUNNING" in leer(f)
               for f in glob.glob("/sys/kernel/debug/dri/*/gt*/uc/huc_info"))

def capacidad_gpu():
    """Si la GPU puede hacer la salida H.264 desde el JPEG de la cámara, y con
    qué codificador. Se calcula una vez: drivers y firmware no cambian sin
    reiniciar el servicio o el equipo."""
    if not _GPU:
        try:
            lista = subprocess.run(["gst-inspect-1.0", "va"], capture_output=True, text=True,
                                   timeout=30, env=entorno_va()).stdout
        except (OSError, subprocess.SubprocessError):
            lista = ""
        elementos = set(re.findall(r"^\s*(va\w+):", lista, re.M))
        # el codificador normal no necesita HuC; el de bajo consumo, sí
        cod = next((e for e in ("vah264enc", "vah264lpenc") if e in elementos), None)
        if not elementos:
            motivo = "sin_gpu"
        elif "vajpegdec" not in elementos:
            motivo = "sin_jpeg"
        elif cod is None:
            motivo = "sin_h264"
        elif cod == "vah264lpenc" and not huc_activo():
            motivo = "necesita_huc"
        else:
            motivo = "ok"
        _GPU.update(disponible=motivo == "ok", motivo=motivo,
                    codificador=cod if motivo == "ok" else None)
    return dict(_GPU)

def decodificador_h264():
    """El de FFmpeg (avdec_h264, varios hilos) si está; si no, openh264dec, que va
    en un solo hilo y no llega a 1080p30 en un Celeron. Por CPU: ver receptor.py."""
    # caché propia: compartir _GPU hacía creer a capacidad_gpu() que ya había medido
    if not _DEC_H264:
        r = subprocess.run(["gst-inspect-1.0", "avdec_h264"], capture_output=True, timeout=30)
        _DEC_H264.append("avdec_h264" if r.returncode == 0 else "openh264dec")
    return _DEC_H264[0]

def energia_rapl():
    """Energía acumulada (µJ) del paquete y de los núcleos según Intel RAPL, y
    el máximo del contador para detectar la vuelta. Mide el SoC (CPU + GPU), no
    el equipo entero."""
    res = {}
    for d in glob.glob("/sys/class/powercap/intel-rapl:*"):
        nombre = leer(f"{d}/name")
        clave = ("paquete" if nombre.startswith("package") else "core" if nombre == "core"
                 else "gpu" if nombre == "uncore" else None)
        if clave and clave not in res:
            try:
                res[clave] = (int(leer(f"{d}/energy_uj")), int(leer(f"{d}/max_energy_range_uj", "0")))
            except ValueError:
                pass
    return res

def vatios(ahora, antes, dt):
    if not antes or dt <= 0:
        return None
    delta = ahora[0] - antes[0]
    if delta < 0:
        delta += antes[1]       # el contador dio la vuelta
    return round(delta / dt / 1e6, 2)

def tarjeta_i915():
    """Directorio sysfs de la GPU Intel (card*), o None."""
    for d in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        if os.path.exists(f"{d}/gt_act_freq_mhz"):
            return d
    return None

def tiempos_gpu():
    """Tiempo de trabajo acumulado (ns) de cada motor de la GPU Intel, sumando
    todos sus clientes (fdinfo de cada proceso que la tiene abierta), y el tiempo
    en reposo RC6 (ms). Sin dependencias: lo da el kernel desde la 5.19."""
    motores, vistos = {}, set()
    for fd in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            if not os.readlink(fd).startswith("/dev/dri/"):
                continue
            pid, _, n = fd[6:].partition("/fd/")
            with open(f"/proc/{pid}/fdinfo/{n}") as f:
                info = dict(l.split(":", 1) for l in f.read().splitlines() if ":" in l)
        except (OSError, ValueError):
            continue
        if info.get("drm-driver", "").strip() != "i915":
            continue
        cliente = (info.get("drm-pdev", "").strip(), info.get("drm-client-id", "").strip())
        if cliente in vistos:
            continue            # varios descriptores pueden ser el mismo cliente
        vistos.add(cliente)
        for k, v in info.items():
            if k.startswith("drm-engine-") and v.strip().endswith("ns"):
                m = k[len("drm-engine-"):]
                motores[m] = motores.get(m, 0) + int(v.split()[0])
    tarjeta = tarjeta_i915()
    rc6 = leer(f"{tarjeta}/power/rc6_residency_ms", "") if tarjeta else ""
    return {"motores": motores, "rc6": int(rc6) if rc6.isdigit() else None}

def uso_gpu(ahora, antes, dt, vatios_gpu):
    """Ocupación de la GPU entre dos medidas: total (lo que no estuvo en reposo
    RC6) y por motor, más frecuencia y potencia."""
    tarjeta = tarjeta_i915()
    if not tarjeta or not antes or dt <= 0:
        return None
    pct = lambda ns: max(0, min(100, round(100 * ns / (dt * 1e9))))
    # un cliente que cierra se lleva su contador: la resta puede salir negativa
    motores = {m: pct(v - antes["motores"].get(m, v)) for m, v in ahora["motores"].items()}
    ocupada = None
    if ahora["rc6"] is not None and antes["rc6"] is not None:
        ocupada = max(0, min(100, round(100 - (ahora["rc6"] - antes["rc6"]) / (dt * 10))))
    mhz = leer(f"{tarjeta}/gt_act_freq_mhz", "")
    tope = leer(f"{tarjeta}/gt_RP0_freq_mhz", "")
    return {"ocupada": ocupada, "motores": motores,
            "mhz": int(mhz) if mhz.isdigit() else None,
            "mhz_max": int(tope) if tope.isdigit() else None,
            "w": vatios_gpu}

def tiempos_cpu():
    v = list(map(int, leer("/proc/stat").splitlines()[0].split()[1:9]))
    return sum(v), v[3] + v[4]
