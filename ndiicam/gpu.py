# NDIICAM — GPU Intel: qué sabe hacer (VA-API, HuC) y cuánto trabaja; energía RAPL y CPU.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
import glob
import json
import os
import re
import resource
import subprocess
import sys
import threading
import time

from nucleo import DATOS, evento, leer


# ------------------------------------------------------------------- fuentes

_GPU = {}
_DEC_H264 = []
_VA = []
_VPP = {}
_VPP_LOCK = threading.Lock()
VPP_ARCHIVO = os.path.join(DATOS, "gpu-vpp.json")
# la GPU se usa si gasta como mucho esta fracción de la CPU que gastaría la CPU sola
VPP_UMBRAL = 0.8
# GPU Intel de bajo consumo en las que ni se mide: un Celeron J3355 (Apollo Lake)
# se colgó entero decodificando JPEG con mucho detalle por GPU (lo reinició el
# watchdog), y aun sin colgarse gastaba 10 veces más CPU. Gemini Lake es de la
# misma familia; Cherry Trail/Braswell, anterior
GPU_SIN_VPP = {0x5a84, 0x5a85, 0x3184, 0x3185, 0x22b0, 0x22b1, 0x22b2, 0x22b3}

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

def elementos_va():
    """Elementos de GStreamer VA que hay (vajpegdec, vapostproc...). Una vez: drivers
    y firmware no cambian sin reiniciar el servicio o el equipo."""
    if not _VA:
        try:
            lista = subprocess.run(["gst-inspect-1.0", "va"], capture_output=True, text=True,
                                   timeout=30, env=entorno_va()).stdout
        except (OSError, subprocess.SubprocessError):
            lista = ""
        _VA.append(frozenset(re.findall(r"^\s*(va\w+):", lista, re.M)))
    return _VA[0]

def capacidad_vpp():
    """Qué más puede hacer la GPU con su procesador de vídeo (vapostproc, VPP):
    convertir sin la CPU. El driver iHD libre de Ubuntu y Debian no lo trae; el
    completo (intel-media-va-driver-non-free), sí.
      jpeg: decodificar el JPEG de la cámara y dejarlo en UYVY para el NDI
      h264: decodificar H.264 (modo puente) y dejarlo en UYVY para el HDMI

    Que exista no basta: la imagen hay que bajarla a memoria y en algunas GPU eso
    es lentísimo (en un Celeron J3355, Apollo Lake, gastaba 10 veces más CPU que
    decodificar por CPU). Así que se mide una vez (medir_vpp) y se usa solo si
    compensa. La medida se guarda y se repite si cambian kernel, driver o GPU;
    mientras se mide, todo va por CPU."""
    e = elementos_va()
    vpp = "vapostproc" in e
    res = {"vpp": vpp, "jpeg": False, "h264": False}
    if not vpp:
        return res
    if ids_gpu() & GPU_SIN_VPP:
        res["vetada"] = True
        return res
    with _VPP_LOCK:
        if not _VPP:
            clave = clave_vpp()
            try:
                with open(VPP_ARCHIVO) as fh:
                    guardado = json.load(fh)
            except (OSError, ValueError):
                guardado = {}
            arranque = leer("/proc/sys/kernel/random/boot_id").strip()
            if guardado.get("midiendo") and guardado.get("arranque") == arranque:
                guardado = {}       # solo se reinició el servicio: medir otra vez
            if guardado.get("clave") == clave and guardado.get("midiendo"):
                # una medida que empezó y nunca acabó, y desde entonces el equipo
                # arrancó de nuevo: se colgó o se reinició midiendo. No se vuelve a probar
                guardado = {"clave": clave, "jpeg": False, "h264": False, "error": "cuelgue",
                            "fecha": int(time.time())}
                _guardar_vpp(guardado)
                evento("gpu_cuelgue")
            if guardado.get("clave") == clave:
                _VPP.update(guardado)
            else:
                _VPP.update(clave=clave, midiendo=True, arranque=arranque)
                # la marca va a disco antes de empezar, por si la GPU cuelga el equipo
                if _guardar_vpp(dict(_VPP)):
                    threading.Thread(target=_medir_aparte, args=(clave,), daemon=True).start()
                else:
                    _VPP.update(midiendo=False, jpeg=False, h264=False, error="disco")
        m = dict(_VPP)
    res["jpeg"] = "vajpegdec" in e and bool(m.get("jpeg"))
    res["h264"] = "vah264dec" in e and bool(m.get("h264"))
    if m.get("midiendo"):
        res["midiendo"] = True
    return res

def equipo_potente():
    """Si el equipo da para hacer varias cosas a la vez (emitir las cámaras
    mientras recibe, varias salidas por cámara...). Hacen falta 4 núcleos, algo
    de memoria y una GPU que decodifique de verdad (medida en capacidad_vpp); sin
    GPU útil, 8 núcleos. Un Atom o un Celeron de 2 núcleos sigue con un modo cada vez."""
    nucleos = os.cpu_count() or 1
    kb = re.search(r"^MemTotal:\s+(\d+)", leer("/proc/meminfo"), re.M)
    memoria = round(int(kb.group(1)) / 1024 ** 2, 1) if kb else 0.0
    vpp = capacidad_vpp()
    gpu = bool(vpp["jpeg"] or vpp["h264"])
    return {"potente": nucleos >= 4 and memoria >= 3.5 and (gpu or nucleos >= 8),
            "nucleos": nucleos, "memoria_gb": memoria, "gpu": gpu,
            "midiendo": bool(vpp.get("midiendo"))}

def edicion():
    """Lite o Pro: el mismo NDIICAM, que en un equipo que da la talla (equipo_potente)
    se convierte en Pro y suma las funciones simultáneas. Con lo que le falta a un
    Lite para ser Pro: nucleos, memoria, gpu."""
    c = equipo_potente()
    falta = []
    if c["nucleos"] < 4:
        falta.append("nucleos")
    if c["memoria_gb"] < 3.5:
        falta.append("memoria")
    if not c["gpu"] and c["nucleos"] < 8:
        falta.append("gpu")
    return {**c, "edicion": "pro" if c["potente"] else "lite", "falta": [] if c["potente"] else falta}

def simultaneo(cfg):
    """Las funciones simultáneas están en marcha: el equipo da para ellas y el
    ajuste (activado por defecto) no las apaga."""
    return bool(cfg.get("simultaneo", True)) and equipo_potente()["potente"]

def ids_gpu():
    """Identificadores PCI de las GPU (0x5a85...)."""
    ids = set()
    for f in glob.glob("/sys/class/drm/card[0-9]*/device/device"):
        try:
            ids.add(int(leer(f).strip(), 16))
        except ValueError:
            pass
    return ids

def _guardar_vpp(datos):
    try:
        with open(VPP_ARCHIVO + ".tmp", "w") as fh:
            json.dump(datos, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(VPP_ARCHIVO + ".tmp", VPP_ARCHIVO)
        return True
    except OSError:
        return False

def clave_vpp():
    """Lo que, si cambia, obliga a medir otra vez: kernel, driver VA y GPU."""
    driver = next(iter(glob.glob("/usr/lib/*/dri/iHD_drv_video.so")), "")
    try:
        st = os.stat(driver)
        driver = f"{st.st_size}-{int(st.st_mtime)}"
    except OSError:
        pass
    gpu = ",".join(hex(i) for i in sorted(ids_gpu()))
    return f"{os.uname().release}|{driver}|{gpu}"

def _medir_aparte(clave):
    """medir_vpp() en otro proceso: si la GPU se cuelga, el servicio no se entera."""
    try:
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "medir"],
                           capture_output=True, text=True, timeout=120, env=entorno_va())
        datos = json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        datos = {"jpeg": False, "h264": False, "error": "medida"}
    datos["clave"] = clave
    _guardar_vpp(datos)
    with _VPP_LOCK:
        _VPP.clear()
        _VPP.update(datos)
    evento("gpu_medida", jpeg=datos.get("jpeg"), h264=datos.get("h264"))

def medir_vpp(fotos=60):
    """CPU que gasta decodificar y dejar en UYVY (lo que necesitan el NDI y el HDMI)
    un JPEG 1080p y un H.264 1080p, por CPU y por GPU. Contenido con mucho detalle,
    que es cuando decodificar cuesta. Devuelve {"jpeg": bool, "h264": bool, ...}."""
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    e = elementos_va()
    origen = ("videotestsrc pattern=snow num-buffers={n}"
              " ! video/x-raw,width=1920,height=1080,framerate=30/1,format={fmt}")

    def generar(desc):
        p = Gst.parse_launch(f"{desc} ! appsink name=s sync=false")
        s = p.get_by_name("s")
        p.set_state(Gst.State.PLAYING)
        bufs, caps = [], None
        while True:
            m = s.emit("try-pull-sample", 10 * Gst.SECOND)
            if m is None:
                break
            caps = m.get_caps()
            bufs.append(m.get_buffer())
        p.set_state(Gst.State.NULL)
        return caps, bufs

    def gasto(caps, bufs, desc):
        """Segundos de CPU de este proceso para pasar bufs por desc; None si falla."""
        p = Gst.parse_launch(f"appsrc name=a format=time ! {desc} ! filesink location=/dev/null")
        a = p.get_by_name("a")
        a.set_property("caps", caps)
        a.set_property("max-bytes", 0)
        uso = resource.getrusage(resource.RUSAGE_SELF)
        antes = uso.ru_utime + uso.ru_stime
        p.set_state(Gst.State.PLAYING)
        for b in bufs:
            a.emit("push-buffer", b)
        a.emit("end-of-stream")
        msg = p.get_bus().timed_pop_filtered(60 * Gst.SECOND,
                                             Gst.MessageType.EOS | Gst.MessageType.ERROR)
        uso = resource.getrusage(resource.RUSAGE_SELF)
        p.set_state(Gst.State.NULL)
        if msg is None or msg.type != Gst.MessageType.EOS:
            return None
        return uso.ru_utime + uso.ru_stime - antes

    res = {"jpeg": False, "h264": False}
    hilos = os.cpu_count() or 2
    uyvy = "video/x-raw,format=UYVY"
    if "vajpegdec" in e and "vapostproc" in e:
        caps, bufs = generar(origen.format(n=fotos, fmt="I420") + " ! jpegenc quality=85")
        if bufs:
            # vajpegdec exige el submuestreo en las caps (ver fuente.muestreo_jpeg)
            caps = Gst.Caps.from_string(
                "image/jpeg,parsed=(boolean)true,width=(int)1920,height=(int)1080,"
                "framerate=(fraction)30/1,sof-marker=(int)0,colorspace=(string)sYUV,"
                "sampling=(string)YCbCr-4:2:0,interlace-mode=(string)progressive")
            cpu = gasto(caps, bufs, f"jpegdec ! videoconvert n-threads={hilos} ! {uyvy}")
            gpu = gasto(caps, bufs, f"vajpegdec ! vapostproc ! {uyvy}")
            res["jpeg_cpu_s"], res["jpeg_gpu_s"] = cpu, gpu
            res["jpeg"] = bool(cpu and gpu is not None and gpu <= VPP_UMBRAL * cpu)
    cod = next((c for c in ("vah264enc", "vah264lpenc") if c in e), None)
    if cod and "vah264dec" in e and "vapostproc" in e:
        caps, bufs = generar(origen.format(n=fotos, fmt="NV12")
                             + f" ! {cod} bitrate=12000 ! h264parse"
                             " ! video/x-h264,stream-format=byte-stream,alignment=au")
        if bufs:
            cpu = gasto(caps, bufs, f"h264parse ! avdec_h264 ! videoconvert n-threads={hilos} ! {uyvy}")
            gpu = gasto(caps, bufs, f"h264parse ! vah264dec ! vapostproc ! {uyvy}")
            res["h264_cpu_s"], res["h264_gpu_s"] = cpu, gpu
            res["h264"] = bool(cpu and gpu is not None and gpu <= VPP_UMBRAL * cpu)
    res["fecha"] = int(time.time())
    return res

def capacidad_gpu():
    """Si la GPU puede hacer la salida H.264 desde el JPEG de la cámara, y con
    qué codificador. Se calcula una vez: drivers y firmware no cambian sin
    reiniciar el servicio o el equipo."""
    if not _GPU:
        elementos = elementos_va()
        # el de bajo consumo (VDEnc) gasta menos, pero sin HuC puede colgar las Gen9;
        # el normal no necesita HuC (con el driver libre no viene)
        huc = "vah264lpenc" in elementos and huc_activo()
        orden = ("vah264lpenc", "vah264enc") if huc else ("vah264enc", "vah264lpenc")
        cod = next((e for e in orden if e in elementos), None)
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


if __name__ == "__main__":
    # python3 gpu.py medir: lo usa capacidad_vpp(), en otro proceso
    if sys.argv[1:] == ["medir"]:
        print(json.dumps(medir_vpp()))
