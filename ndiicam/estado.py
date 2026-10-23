# NDIICAM — el estado que ve el panel, medido cada 2 s.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
import glob
import os
import re
import shutil
import socket
import time

import nucleo
from nucleo import (ESTADO, FPS_VALIDOS, GRABACIONES, LIMITE_ENTRADAS, LIMITE_RUTAS, NOMBRE_GRABACION,
    PUERTO_HLS, PUERTO_RTMP, PUERTO_SRT, RESOLUCIONES, RUTA_PROGRAMA, VERSION, bytes_red,
    conexiones_por_pid, evento, ip_de, latido, leer, nombre_de)
from camaras import camaras as camaras_conectadas, modo_consola_activo, pantallas
from gpu import (capacidad_gpu, capacidad_vpp, decodificador_h264, energia_rapl,
    edicion, simultaneo, tiempos_cpu, tiempos_gpu, uso_gpu, vatios)
from puente import PUENTE
from supervisor import ocultar_clave_rtmp
import rutas as rutas_mod
import sistema


def medidor():
    prev = None
    while True:
        try:
            t, cpu, rapl, gpu = time.time(), tiempos_cpu(), energia_rapl(), tiempos_gpu()
            tx, rx = bytes_red("tx"), bytes_red("rx")
            for f in nucleo.FUENTES.lista():
                f.medir(t)
            nucleo.FUENTES.receptor.medir(t)
            nucleo.FUENTES.reemision.medir(t)
            nucleo.FUENTES.programa.medir(t)
            nucleo.FUENTES.motor.medir(t)
            if prev:
                dt = t - prev["t"]
                potencia = {k: vatios(v, prev["rapl"].get(k), dt) for k, v in rapl.items()}
                ESTADO.update(construir_estado(
                    (tx - prev["tx"]) * 8 / dt / 1e6, (rx - prev["rx"]) * 8 / dt / 1e6,
                    100 * (1 - (cpu[1] - prev["cpu"][1]) / max(1, cpu[0] - prev["cpu"][0])),
                    potencia, uso_gpu(gpu, prev["gpu"], dt, potencia.get("gpu"))))
            prev = {"t": t, "tx": tx, "rx": rx, "cpu": cpu, "rapl": rapl, "gpu": gpu}
        except Exception as ex:
            evento("fallo_interno", detalle=str(ex))
        latido("medidor")
        time.sleep(2)

def estado_receptor():
    rx = nucleo.FUENTES.receptor
    if rx.destino is None:
        return None
    c = rx.c if rx.proc is not None else {}
    return {"fuente": rx.destino, "estado": rx.estado, "error": rx.error, "desde": rx.desde,
            "ancho": c.get("ancho"), "alto": c.get("alto"), "fps_fuente": c.get("fps"),
            "fps": rx.stats["fps"] if rx.estado == "recibiendo" else 0,
            "audio": bool(rx.obj and rx.obj.get("audio")),
            "audio_activo": rx.estado == "recibiendo" and rx.stats["audio_activo"],
            "nivel": rx.stats["audio_db"] if rx.estado == "recibiendo" and rx.stats["audio_activo"] else None,
            "latencia_ms": rx.stats["latencia_ms"] if rx.estado == "recibiendo" else None,
            "audio_fallido": rx.audio_fallido is not None,
            # quién escala y decodifica: el plano y la GPU, o la CPU
            "escala": None if not (rx.obj and rx.obj.get("pantalla")) else
            "plano" if rx.obj.get("plano") or rx.obj.get("origen") == "puente" else "cpu",
            "gpu": bool(rx.obj and rx.obj.get("gpu_h264"))}

def grabaciones():
    try:
        nombres = [n for n in os.listdir(GRABACIONES) if NOMBRE_GRABACION.match(n)]
    except OSError:
        return {"lista": [], "libre": None}
    lista = []
    for n in nombres:
        try:
            st = os.stat(os.path.join(GRABACIONES, n))
        except OSError:
            continue
        lista.append({"nombre": n, "bytes": st.st_size, "fecha": st.st_mtime})
    lista.sort(key=lambda x: x["fecha"], reverse=True)
    return {"lista": lista, "libre": shutil.disk_usage(GRABACIONES).free}

def estado_rutas():
    """Todo lo que necesita el panel de rutas: entradas (con lo que llega por la red),
    rutas guardadas, sus errores y cómo va cada una y cada salida."""
    cfg = nucleo.CONF.get()
    mot = nucleo.FUENTES.motor
    # siempre desde la configuración (lo recién guardado); lo que corre, del motor
    cams = {c["id"]: c for c in camaras_conectadas()}
    entradas = mot.entradas_actuales(cams, cfg)
    limpias, errores = rutas_mod.validar(cfg.get("rutas") or [], entradas)
    est, activas, pausadas = {}, [], []
    if cfg.get("motor") == "rutas" and getattr(mot, "limpias", None) is not None:
        est, activas, pausadas = mot.estado(), mot.ultimo["activas"], mot.ultimo["pausadas"]
        for rid, e in mot.errores.items():      # los que solo ve el motor (sin GPU...)
            errores[rid] = sorted(set(errores.get(rid, [])) | set(e))
    ents = []
    for e in entradas:
        e = dict(e)
        if e["tipo"] == "ingesta":
            e["llega"] = PUENTE.ruta_info(f"live/{e['clave']}")
        if e["tipo"] == "usb_cam" and e["id"] in cams:
            # sus ajustes (resolución, fps, formato, latencia) y lo que admite la
            # cámara: con rutas no hay otra pestaña donde cambiarlos
            aj = nucleo.CONF.fuente(e["id"])
            modos = cams[e["id"]].get("modos") or []
            e["ajustes"] = {k: aj[k] for k in ("resolucion", "fps", "formato", "latencia")}
            e["admite"] = {"720p": any((m[0], m[1]) == (1280, 720) for m in modos),
                           "1080p": any((m[0], m[1]) == (1920, 1080) for m in modos),
                           "fps": sorted({m[2] for m in modos if m[2] in FPS_VALIDOS}),
                           "mjpeg": any(m[3] == "MJPG" for m in modos), "crudo": any(m[3] == "YUYV" for m in modos)}
        ents.append(e)
    # la clave de YouTube no sale del equipo: el panel la ve tapada (y al
    # devolverla tapada, se conserva la guardada)
    for r in limpias:
        for sal in r.get("salidas") or []:
            if sal.get("tipo") == "rtmp" and sal.get("url"):
                sal["url"] = ocultar_clave_rtmp(sal["url"])
    # las pantallas con monitor y qué ruta tiene cada una
    de_ruta = {pr["hdmi"]: pr["nombre_ruta"] for pr in (mot.ultimo.get("procesos") or [])
               if isinstance(pr.get("hdmi"), str)}
    de_ruta_id = {pr["hdmi"]: pr["ruta"] for pr in (mot.ultimo.get("procesos") or [])
                  if isinstance(pr.get("hdmi"), str)}
    pants = [{"conector": p["conector"], "id": p["id"], "modo": p["modo"],
              "ruta": de_ruta_id.get(p["conector"]), "ruta_nombre": de_ruta.get(p["conector"])}
             for p in pantallas() if p["conectado"] and p["id"]]
    return {"motor": cfg.get("motor", "modos"), "entradas": ents, "rutas": limpias, "pantallas": pants,
            "errores": errores, "estado": est, "activas": activas, "pausadas": pausadas,
            "red_ndi": nucleo.BUSCADOR.lista() if nucleo.BUSCADOR else [],
            "puertos": {"rtmp": PUERTO_RTMP, "srt": PUERTO_SRT, "hls": PUERTO_HLS},
            "limite_lite": rutas_mod.LIMITE_LITE, "pro": simultaneo(cfg),
            "limite_rutas": LIMITE_RUTAS, "limite_entradas": LIMITE_ENTRADAS}

def estado_programa(cfg):
    """El programa final del puente (Pro): vídeo del ATEM + audio de la consola."""
    pr = nucleo.FUENTES.programa
    activo = pr.estado == "activa"
    return {"disponible": simultaneo(cfg), "estado": pr.estado, "error": pr.error,
            "desde": pr.desde, "fps": pr.stats["fps"] if activo else 0,
            "mbps": pr.stats["mbps"] if activo else 0,
            "audio_activo": activo and pr.stats["audio_activo"],
            "nivel": pr.stats.get("audio_db") if activo else None,
            "consola": cfg["programa_audio"], "retraso": cfg["programa_retraso"],
            "grabar": cfg["programa_grabar"], "hdmi": cfg["puente_hdmi"]}

def estado_reemision(cfg):
    r = nucleo.FUENTES.reemision
    obj = r.obj or {}
    activa = r.estado == "activa"
    return {"disponible": simultaneo(cfg), "estado": r.estado, "error": r.error,
            "desde": r.desde, "fuente": cfg["reem_fuente"],
            "fps": r.stats["fps"] if activa else 0, "mbps": r.stats["mbps"] if activa else 0,
            "audio_activo": activa and r.stats["audio_activo"],
            "salidas": [{"modo": s["modo"], "puerto": s.get("puerto")}
                        for s in obj.get("salidas", [])]}

def construir_estado(tx_mbps, rx_mbps, cpu_pct, potencia, gpu_uso=None):
    host = socket.gethostname()
    cfg = nucleo.CONF.get()
    conexiones = conexiones_por_pid()
    fuentes, total_rx = [], 0
    for f in nucleo.FUENTES.lista():
        aj = nucleo.CONF.fuente(f.cam["id"])
        obj = f.obj or {}
        pid = f.proc.pid if f.proc else None
        rx = sorted(conexiones.get(pid, set())) if f.estado == "emitiendo" else []
        total_rx += len(rx)
        audio_dev = next((a for a in nucleo.FUENTES.audio if a["dev"] == obj.get("audio")), None)
        res_sel = (obj["ancho"], obj["alto"]) if obj else None
        en_marcha = f.estado in ("iniciando", "emitiendo")
        fuentes.append({
            "id": f.cam["id"], "camara": f.cam["nombre"], "dispositivo": f.cam["dev"],
            # sin NDI (solo monitor) no "emite": está en el HDMI
            "estado": "en_hdmi" if f.estado == "emitiendo" and obj.get("solo_monitor") else f.estado,
            "error": f.error, "desde": f.desde,
            "nombre_ndi": f"{host.upper()} ({obj['nombre']})" if obj and not obj.get("solo_monitor") else None,
            "modo": f"{obj['ancho']}×{obj['alto']} @{obj['fps']}" if obj else None,
            "formato": obj.get("fmt"),
            "hilos": f.c.get("hilos", 1) if en_marcha else None,
            "fps_camara": f.stats["fps_camara"] if en_marcha else 0,
            "fps_enviados": f.stats["fps_enviados"] if en_marcha else 0,
            "mbps_camara": f.stats["mbps_camara"] if en_marcha else 0,
            "descartes": f.stats["descartes"] if en_marcha else 0,
            "audio": {"dispositivo": audio_dev["nombre"] if audio_dev else None,
                      "activo": bool(obj.get("audio")) and f.stats["audio_activo"],
                      "nivel": f.stats["audio_db"] if en_marcha and f.stats["audio_activo"] else None,
                      "fallido": f.audio_fallido is not None,
                      "disponible": any(a["usb"] and a["usb"] == f.cam["usb"]
                                        for a in nucleo.FUENTES.audio)},
            "receptores": [{"ip": ip, "nombre": nombre_de(ip)} for ip in rx],
            "conexiones_ndi": f.c.get("conexiones") if en_marcha else None,
            "latencia_ms": f.stats["latencia_ms"] if en_marcha else None,
            "tally": f.tally if en_marcha else None,
            "hdmi": bool(obj.get("hdmi")), "hdmi_fallido": f.hdmi_fallido,
            "calibracion": f.calibracion,
            "gpu": {"modo": (obj.get("gpu") or {}).get("modo", "no"),
                    "puerto": (obj.get("gpu") or {}).get("puerto"),
                    "estado": f.gpu["estado"] if en_marcha and obj.get("gpu") else None,
                    "error": f.gpu["error"],
                    "fps": f.stats["fps_gpu"] if en_marcha else 0,
                    "mbps": f.stats["mbps_gpu"] if en_marcha else 0},
            # salidas además de la principal (equipo potente)
            "gpu_extra": [{"modo": g["modo"], "puerto": g.get("puerto"),
                           **(f.gpu_extra.get(g["modo"]) if en_marcha else {"estado": None, "error": ""})}
                          for g in obj.get("gpu_extra") or []],
            "ajustes": {**aj, "rtmp_url": ocultar_clave_rtmp(aj["rtmp_url"])},
            "admite": {k: any(m[0] == v[0] and m[1] == v[1] for m in f.cam["modos"])
                       for k, v in RESOLUCIONES.items()},
            "fps_disponibles": sorted({m[2] for m in f.cam["modos"]
                                       if res_sel and (m[0], m[1]) == res_sel
                                       and m[2] in FPS_VALIDOS}),
        })
    estados = [x["estado"] for x in fuentes]
    receptor = cfg["modo"] in ("receptor", "puente")
    rx_estado = estado_receptor()
    monitor = any(p["conectado"] for p in pantallas())
    puente_info = None
    if cfg["modo"] == "puente":
        puente_info = {**PUENTE.info(), "instalado": PUENTE.disponible(),
                       "clave": cfg["puente_clave"], "puerto_rtmp": PUERTO_RTMP,
                       "puerto_srt": PUERTO_SRT,
                       "decodificador": "vah264dec" if (nucleo.FUENTES.receptor.obj or {}).get("gpu_h264")
                       else decodificador_h264(),
                       # también como fuente NDI (equipo potente)
                       "ndi": (f"{host.upper()} ({nucleo.FUENTES.receptor.obj['ndi_nombre']})"
                               if (nucleo.FUENTES.receptor.obj or {}).get("ndi_nombre") else None),
                       "ndi_disponible": simultaneo(cfg), "ndi_activado": cfg["puente_ndi"],
                       "programa": estado_programa(cfg), "puerto_hls": PUERTO_HLS,
                       "ruta_programa": RUTA_PROGRAMA}
    if cfg["modo"] == "puente":
        if not puente_info["instalado"]:
            agregado = "rx_error"
        elif not monitor and not (cfg["puente_ndi"] and simultaneo(cfg)):
            agregado = "sin_monitor"
        elif not puente_info.get("publicando"):
            agregado = "esperando_atem"
        else:
            agregado = {"recibiendo": "recibiendo", "error": "rx_error"}.get(
                rx_estado["estado"] if rx_estado else "", "conectando")
    elif receptor:
        # en modo receptor, el estado general es el de la recepción
        if not monitor:
            agregado = "sin_monitor"
        elif not cfg["rx_fuente"]:
            agregado = "sin_fuente"
        else:
            agregado = {"recibiendo": "recibiendo", "error": "rx_error"}.get(
                rx_estado["estado"] if rx_estado else "", "conectando")
    elif not fuentes:
        agregado = "sin_camara"
    elif "emitiendo" in estados:
        agregado = "emitiendo"
    elif "en_hdmi" in estados:
        agregado = "en_hdmi"
    elif "iniciando" in estados:
        agregado = "iniciando"
    elif "error" in estados:
        agregado = "error"
    else:
        agregado = "desactivada"
    rutas_info = None
    if cfg.get("motor") == "rutas":
        # con rutas, el estado general es el de las rutas
        mot = nucleo.FUENTES.motor
        est = {rid: e["estado"] for rid, e in mot.estado().items()}
        vals = list(est.values())
        agregado = ("sin_rutas" if not vals else "emitiendo" if "en_marcha" in vals
                    else "error" if "error" in vals else "iniciando")
        rutas_info = {"total": len(vals), "en_marcha": vals.count("en_marcha"),
                      "nombres": [r["nombre"] for r in getattr(mot, "limpias", [])
                                  if est.get(r["id"]) == "en_marcha"]}
    devs = nucleo.RED.dispositivos()
    wifi = nucleo.RED.info_wifi()
    eth = next((d for d in devs if d["tipo"] == "ethernet"), None)
    eth_ok = bool(eth and eth["estado"] == "connected")
    eth_info = None
    if eth:
        vel = leer(f"/sys/class/net/{eth['dev']}/speed", "")
        eth_info = {"conectada": eth_ok, "ip": ip_de(eth["dev"]) if eth_ok else None,
                    "velocidad": int(vel) if vel.isdigit() and int(vel) > 0 else None,
                    "dev": eth["dev"], "mac": leer(f"/sys/class/net/{eth['dev']}/address"),
                    "ip_config": nucleo.RED.ip_config_cable()}
    if wifi and nucleo.RED.wifi:
        wifi["mac"] = leer(f"/sys/class/net/{nucleo.RED.wifi}/address")
    if eth_ok:
        modo, capacidad = "ethernet", 0.9 * (eth_info["velocidad"] or 100)
    elif wifi:
        # en WiFi, lo útil para TCP ronda la mitad de la velocidad del enlace, en el
        # sentido que importa: de subida al emitir, de bajada al recibir
        enlace = wifi["rx_mbit"] if receptor else wifi["tx_mbit"]
        modo, capacidad = "wifi", 0.5 * (enlace or 0)
    else:
        modo, capacidad = "sin_red", None
    mem = dict(re.findall(r"^(\w+):\s+(\d+)", leer("/proc/meminfo"), re.M))
    temps = [int(leer(z, "0")) / 1000 for z in
             glob.glob("/sys/class/thermal/thermal_zone*/temp")]
    return {
        "version": VERSION,
        "gpu": {**capacidad_gpu(), **capacidad_vpp()},
        # si da para varias cosas a la vez, y si están en marcha
        "capacidad": {**edicion(), "simultaneo": simultaneo(cfg)},
        "equipo": {"nombre": host,
                   "uptime": float(leer("/proc/uptime", "0").split()[0]),
                   "cpu": round(cpu_pct), "nucleos": os.cpu_count(),
                   "potencia_w": potencia.get("paquete"), "potencia_nucleos_w": potencia.get("core"),
                   "gpu": gpu_uso,
                   "temp": round(max(temps), 1) if temps else None,
                   "watchdog": sistema.watchdog(), "hora": sistema.hora(),
                   "arranque": sistema.MOTIVO,
                   "ram_pct": round(100 * (1 - int(mem.get("MemAvailable", 0))
                                           / max(1, int(mem.get("MemTotal", 1)))))},
        "fuentes": fuentes,
        "audio_dispositivos": [{"id": a["id"], "nombre": a["nombre"], "usb": bool(a["usb"])}
                               for a in nucleo.FUENTES.audio],
        "modo": cfg["modo"],
        "puente": puente_info,
        "receptor": {"fuente": cfg["rx_fuente"], "estado": rx_estado,
                     # la lista también sirve para elegir qué reemitir (equipo potente)
                     "red_ndi": nucleo.BUSCADOR.lista() if receptor or simultaneo(cfg) else []},
        "reemision": estado_reemision(cfg),
        "hdmi": {"activa": cfg["hdmi"], "fuente": cfg["hdmi_fuente"], "mostrando": nucleo.FUENTES.hdmi_para,
                 "pantallas": pantallas(), "consola": cfg["hdmi_consola"],
                 "consola_activa": modo_consola_activo(),
                 "reinicio_pendiente": cfg["hdmi_consola"] != modo_consola_activo()},
        "rutas": rutas_info,
        "emision": {"estado": agregado, "emitiendo": estados.count("emitiendo"),
                    "activas": sum(1 for x in fuentes if x["ajustes"]["activa"]),
                    "receptores": total_rx},
        "red": {"modo": modo, "wifi": wifi, "ethernet": eth_info,
                "tiene_wifi": bool(nucleo.RED.wifi),
                "tx_mbps": round(tx_mbps, 1), "rx_mbps": round(rx_mbps, 1),
                # el tráfico que importa: el que sale al emitir, el que entra al recibir
                "trafico_mbps": round(rx_mbps if receptor else tx_mbps, 1),
                "sentido": "entrada" if receptor else "salida",
                "capacidad_mbps": round(capacidad) if capacidad else None,
                "ip": nucleo.RED.ip_config(),
                "ap": {"activo": nucleo.RED.ap_activo, "modo": nucleo.RED.modo_ap,
                       "ssid": cfg["ap_ssid"], "clave": cfg["ap_clave"],
                       "clientes": nucleo.RED.clientes_ap() if nucleo.RED.ap_activo else 0,
                       "simultaneo": not nucleo.RED.simultaneo_falla,
                       "forzado_hasta": nucleo.RED.forzado_hasta
                       if nucleo.RED.forzado_hasta > time.time() else None}},
        "actualizacion": nucleo.ACTUALIZADOR.info,
    }
