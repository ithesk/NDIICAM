# NDIICAM — rutas: entradas → (imagen + audio ajustado) → salidas.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""El montaje como un enrutador. Una ENTRADA da imagen, audio o las dos: una cámara
USB (con o sin micrófono), una interfaz de audio USB, una fuente NDI de la red o una
emisión que llega por RTMP/SRT (un ATEM, OBS, un móvil). Una RUTA coge la imagen de
una entrada y el audio de esa u otra, con ganancia, retraso y silencio, y lo manda a
varias SALIDAS: NDI, el monitor HDMI, OBS/TV/móvil (RTMP, HLS), SRT, RTMP hacia
fuera (YouTube) y grabación.

Aquí no se toca GStreamer: plan() traduce las rutas a los procesos que ya existen
(fuente.py para las cámaras, programa.py para lo que llega por la red, reemision.py
y receptor.py para las fuentes NDI) y a lo que tiene que servir MediaMTX, que hace
de bus interno: cada ruta con salidas de red publica UN programa en live/<camino>,
y de ahí lo leen OBS, la TV y SRT; un relevo (relevo.py) lo graba o lo reenvía
hacia YouTube. Así, si se cae una salida de red, las demás siguen."""
import re
import unicodedata

TIPOS_ENTRADA = ("usb_cam", "usb_audio", "ndi", "ingesta")
TIPOS_SALIDA = ("ndi", "hdmi", "obs", "srt", "rtmp", "grabar")
# las que salen del programa publicado en MediaMTX
DE_RED = ("obs", "srt", "rtmp", "grabar")
LIMITE_LITE = 1         # rutas a la vez en un equipo Lite
GANANCIA = (-24, 12)    # dB
RETRASO_MAX = 1000      # ms
URL_RTMP = re.compile(r"^rtmps?://[^\s\"'<>]{3,500}$")


def camino(nombre, usados=()):
    """Nombre corto para la URL de una ruta: «Culto de las 11» → culto-de-las-11."""
    s = unicodedata.normalize("NFKD", nombre or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "ruta"
    base, n = s, 2
    while s in usados:
        s, n = f"{base}-{n}", n + 1
    return s


def da_imagen(e):
    return e["tipo"] in ("usb_cam", "ndi", "ingesta")


def da_audio(e):
    if e["tipo"] == "usb_cam":
        return bool(e.get("audio_dev"))      # el micrófono de la cámara, si tiene
    return e["tipo"] in ("usb_audio", "ndi", "ingesta")


def validar(rutas, entradas):
    """Comprueba una lista de rutas tal como llega del panel. Devuelve (rutas
    normalizadas, {ruta_id: [errores]}). Los errores no impiden guardar: la ruta
    se queda parada y el panel dice por qué."""
    por_id = {e["id"]: e for e in entradas}
    vistas, caminos, nombres_ndi, salida_ids = set(), set(), set(), set()
    hdmi_de = None
    limpias, errores = [], {}
    for r in rutas:
        rid = str(r.get("id") or "")[:40]
        if not rid or rid in vistas:
            continue
        vistas.add(rid)
        err = []
        nr = {"id": rid, "nombre": str(r.get("nombre") or "Ruta")[:60],
              "video": str(r.get("video") or ""), "audio": str(r.get("audio") or ""),
              "ganancia": max(GANANCIA[0], min(GANANCIA[1], int(r.get("ganancia") or 0))),
              "retraso": max(0, min(RETRASO_MAX, int(r.get("retraso") or 0))),
              "mudo": bool(r.get("mudo")), "activa": r.get("activa", True) is not False,
              "salidas": []}
        nr["camino"] = camino(r.get("camino") or nr["nombre"], caminos)
        caminos.add(nr["camino"])
        v = por_id.get(nr["video"])
        if not v:
            err.append("sin_imagen" if not nr["video"] else "imagen_no_esta")
        elif not da_imagen(v):
            err.append("imagen_no_valida")
        a = por_id.get(nr["audio"]) if nr["audio"] else None
        if nr["audio"] and not a:
            err.append("audio_no_esta")
        elif a and not da_audio(a):
            err.append("audio_no_valido")
        for s in r.get("salidas") or []:
            tipo = s.get("tipo")
            if tipo not in TIPOS_SALIDA:
                continue
            sid = str(s.get("id") or f"{rid}-{tipo}")[:40]
            while sid in salida_ids:
                sid += "x"
            salida_ids.add(sid)
            ns = {"id": sid, "tipo": tipo}
            if tipo == "ndi":
                ns["nombre"] = str(s.get("nombre") or nr["nombre"])[:48].strip() or "Programa"
                if ns["nombre"].lower() in nombres_ndi:
                    err.append("ndi_repetido")
                nombres_ndi.add(ns["nombre"].lower())
                if v and v["tipo"] == "ndi":
                    err.append("ndi_a_ndi")       # ya es una fuente NDI: no se reemite como NDI
            elif tipo == "hdmi":
                if hdmi_de is not None:
                    err.append("hdmi_ocupado")    # un solo monitor: lo tiene la primera ruta
                    continue
                hdmi_de = rid
            elif tipo == "grabar":
                ns["formato"] = "mkv" if str(s.get("formato") or "").lower() == "mkv" else "mp4"
            elif tipo == "rtmp":
                ns["url"] = str(s.get("url") or "").strip()
                if not URL_RTMP.match(ns["url"]):
                    err.append("url_rtmp_invalida")
            if any(x["tipo"] == tipo for x in nr["salidas"]) and tipo != "rtmp":
                continue                          # dos iguales en la misma ruta no tienen sentido
            nr["salidas"].append(ns)
        if not nr["salidas"]:
            err.append("sin_salidas")
        limpias.append(nr)
        if err:
            errores[rid] = sorted(set(err))
    return limpias, errores


def plan(rutas, entradas, potente, errores=None):
    """Qué procesos tienen que correr para estas rutas y qué sirve MediaMTX.

    entradas: las detectadas (USB) y las de la red, con id, tipo y lo suyo (dev,
    audio_dev, fuente NDI, clave de la ingesta). potente: edición Pro (si no, solo
    LIMITE_LITE rutas a la vez). Devuelve {"procesos": [...], "mediamtx": {...},
    "activas": [...], "pausadas": [...]}; cada proceso lleva una "clave" estable para
    saber si ya corre y la "ruta" a la que pertenece."""
    errores = errores or {}
    por_id = {e["id"]: e for e in entradas}
    procesos, ingestas, publicaciones, activas, pausadas = [], [], [], [], []
    for e in entradas:
        if e["tipo"] == "ingesta" and e.get("clave"):
            ingestas.append(f"live/{e['clave']}")
    for r in rutas:
        if not r["activa"] or errores.get(r["id"]) and any(
                x in errores[r["id"]] for x in ("sin_imagen", "imagen_no_esta", "imagen_no_valida",
                                                 "sin_salidas")):
            pausadas.append(r["id"])
            continue
        if not potente and len(activas) >= LIMITE_LITE:
            pausadas.append(r["id"])
            continue
        activas.append(r["id"])
        v = por_id[r["video"]]
        a = por_id.get(r["audio"]) if r["audio"] else None
        tipos = {s["tipo"] for s in r["salidas"]}
        ndi = next((s["nombre"] for s in r["salidas"] if s["tipo"] == "ndi"), None)
        if "ndi_a_ndi" in errores.get(r["id"], ()):
            ndi = None
        hdmi = "hdmi" in tipos       # validar() ya dejó el monitor a una sola ruta
        publicar = f"live/{r['camino']}" if tipos & set(DE_RED) else None
        # el audio: el de la propia entrada (micrófono de la cámara, el del NDI o el
        # que trae el ATEM) o el de otra (la consola), con sus ajustes
        propio = a is not None and a["id"] == v["id"]
        audio = {"de": None if a is None else "propio" if propio else "aparte",
                 "dev": (a.get("audio_dev") if a and a["tipo"] == "usb_cam" else
                         a.get("dev") if a and a["tipo"] == "usb_audio" else None),
                 "ganancia": r["ganancia"], "mudo": r["mudo"],
                 # el retraso es para alinear un audio que viene por otro camino
                 "retraso": r["retraso"] if a is not None else 0}
        if a is not None and a["tipo"] in ("ndi", "ingesta") and not propio:
            # audio de otra fuente de red: sin soporte todavía (se usa el de la imagen)
            audio = {**audio, "de": "propio" if da_audio(v) else None, "dev": None}
        base = {"ruta": r["id"], "nombre_ruta": r["nombre"]}
        if v["tipo"] == "usb_cam":
            procesos.append({**base, "clave": f"cam:{r['id']}", "clase": "camara", "cam": v["id"],
                             "ndi": ndi, "hdmi": hdmi, "publicar": publicar, "audio": audio})
        elif v["tipo"] == "ingesta":
            # el programa final: vídeo del ATEM sin recodificar + el audio que toque
            prog = f"live/{r['camino']}"
            procesos.append({**base, "clave": f"prog:{r['id']}", "clase": "programa",
                             "entrada": f"live/{v['clave']}", "publicar": prog, "audio": audio})
            if publicar is None:
                publicar = prog         # lo publica igual: de ahí leen el NDI y el HDMI
            if ndi or hdmi:
                procesos.append({**base, "clave": f"rx:{r['id']}", "clase": "receptor",
                                 "origen": "rtmp", "fuente": prog, "ndi": ndi, "hdmi": hdmi})
        else:   # fuente NDI de la red
            if hdmi:
                procesos.append({**base, "clave": f"rx:{r['id']}", "clase": "receptor",
                                 "origen": "ndi", "fuente": v["fuente"], "ndi": None, "hdmi": True,
                                 "audio": audio})
            if publicar:
                procesos.append({**base, "clave": f"reem:{r['id']}", "clase": "reemision",
                                 "fuente": v["fuente"], "publicar": publicar, "audio": audio})
        if publicar:
            publicaciones.append({"camino": publicar, "prefijo": r["camino"]})
        # lo que sale del programa publicado: un proceso por salida (si una cae, las
        # demás siguen); OBS, la TV y SRT leen de MediaMTX directamente
        for s in r["salidas"]:
            if s["tipo"] == "rtmp" and URL_RTMP.match(s.get("url", "")):
                procesos.append({**base, "clave": f"relevo:{r['id']}:{s['id']}", "clase": "relevo",
                                 "desde": publicar, "modo": "rtmp", "url": s["url"]})
            elif s["tipo"] == "grabar":
                procesos.append({**base, "clave": f"relevo:{r['id']}:{s['id']}", "clase": "relevo",
                                 "desde": publicar, "modo": "grabar", "prefijo": r["camino"],
                                 "formato": s.get("formato", "mp4")})
    return {"procesos": procesos, "activas": activas, "pausadas": pausadas,
            "mediamtx": {"ingestas": ingestas, "publicaciones": publicaciones,
                         "necesario": bool(ingestas or publicaciones)}}


def desde_modos(cfg, camaras, fuente_cfg, audios):
    """Las rutas equivalentes a lo que hacían los modos, para pasar al motor de rutas
    sin perder nada: el ATEM sigue con su clave, las cámaras con su NDI y el receptor
    con su fuente. camaras: {id: cam}; fuente_cfg(id): ajustes de esa cámara;
    audios: interfaces de audio (id ALSA). Devuelve (entradas_red, rutas)."""
    red, lista = [], []
    modo = cfg.get("modo", "emisor")
    hdmi_cam = cfg.get("hdmi_fuente") if cfg.get("hdmi") else None
    if modo == "puente" and cfg.get("puente_clave"):
        red.append({"id": "atem", "tipo": "ingesta", "nombre": "ATEM", "clave": cfg["puente_clave"]})
        consola = cfg.get("programa_audio") or ""
        salidas = [{"tipo": "obs"}, {"tipo": "ndi", "nombre": "Puente"}]
        if cfg.get("programa_grabar"):
            salidas.append({"tipo": "grabar", "formato": "mkv"})
        if cfg.get("puente_hdmi", "programa") == "programa":
            salidas.append({"tipo": "hdmi"})
        lista.append({"id": "programa", "nombre": "Programa", "video": "atem",
                      "audio": f"audio:{consola}" if consola in audios else "atem",
                      "retraso": cfg.get("programa_retraso", 0) if consola else 0, "salidas": salidas})
        if cfg.get("puente_hdmi") in camaras:
            hdmi_cam = cfg["puente_hdmi"]
    if modo == "receptor" and cfg.get("rx_fuente"):
        red.append({"id": "ndi-rx", "tipo": "ndi", "nombre": cfg["rx_fuente"], "fuente": cfg["rx_fuente"]})
        lista.append({"id": "receptor", "nombre": "Receptor", "video": "ndi-rx", "audio": "ndi-rx",
                      "salidas": [{"tipo": "hdmi"}]})
    # las cámaras: en emisor siempre; en receptor y puente, las que seguían emitiendo
    # (edición Pro) o la que iba al monitor
    for cid, cam in camaras.items():
        aj = fuente_cfg(cid)
        if not aj.get("activa", True):
            continue
        salidas = []
        if not aj.get("solo_monitor") and (modo == "emisor" or cfg.get("simultaneo", True)):
            salidas.append({"tipo": "ndi", "nombre": aj.get("ndi_nombre") or cam["nombre"]})
        if cid == hdmi_cam or (modo == "emisor" and cfg.get("hdmi") and hdmi_cam in (None, "auto")
                               and not any(s["tipo"] == "hdmi" for r in lista for s in r["salidas"])):
            salidas.append({"tipo": "hdmi"})
            hdmi_cam = cid
        if aj.get("gpu_salida") == "grabar":
            salidas.append({"tipo": "grabar", "formato": "mp4"})
        if aj.get("gpu_salida") == "rtmp" and aj.get("rtmp_url"):
            salidas.append({"tipo": "rtmp", "url": aj["rtmp_url"]})
        if not salidas:
            continue
        audio = aj.get("audio", "auto")
        lista.append({"id": "cam-" + camino(cam["nombre"]), "nombre": cam["nombre"], "video": cid,
                      "audio": cid if audio == "auto" else (f"audio:{audio}" if audio in audios else ""),
                      "retraso": aj.get("retraso_audio", 0), "salidas": salidas})
    return red, lista
