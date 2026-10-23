# NDIICAM — validación de los ajustes: las mismas reglas para el panel y para restaurar una copia.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Funciones puras: reciben lo que llega (del panel o de una copia de seguridad)
y devuelven lo que se puede guardar y un código de error por campo. Lo que
depende del equipo (qué cámaras y entradas de audio hay, si hay GPU, si el
cambio pide la contraseña actual) lo comprueba el panel después.

    validar_config(d)  -> (cambios, errores)     errores: {campo: codigo}
    validar_fuente(d)  -> (cambios, errores)
"""
import re

from nucleo import (AP_CLAVE_ANTIGUA, CLAVE_PUENTE, FORMATOS, FPS_VALIDOS, GRABAR_CUANDO, HORA,
    IDIOMAS, LATENCIAS, MODOS, MODOS_CONSOLA, RESOLUCIONES, RETRASO_MAX_MS, SALIDAS_EXTRA,
    SALIDAS_GPU, URL_RTMP, NOMBRE_NDI)
from cortafuegos import puertos_extra

RESUMEN_CLAVE = re.compile(r"^pbkdf2\$[0-9a-f]{16}\$[0-9a-f]{64}$")


def _entero(v, minimo, maximo):
    """int(v) dentro de [minimo, maximo], o None si no vale."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if minimo <= v <= maximo else None


def _url_rtmp(v):
    """'' (sin URL), la URL, u 'oculta' si trae los puntos con los que el panel
    enseña la guardada (entonces se conserva la que había)."""
    v = str(v).strip()
    if "•" in v:
        return "oculta"
    if v and not URL_RTMP.match(v):
        return None
    return v


def _lista(v, admitidas):
    if not isinstance(v, list) or any(str(x) not in admitidas for x in v):
        return None
    return sorted({str(x) for x in v})


# cada campo: (función que devuelve el valor limpio o None, código de error).
# El orden es el que tenía el panel: el primer error es el que se responde
REGLAS_CONFIG = (
    ("hdmi", lambda v: bool(v), "modo_invalido"),
    ("modo", lambda v: v if v in MODOS else None, "modo_invalido"),
    ("decodificadores", lambda v: ("auto" if str(v) == "auto" else _entero(v, 1, 4)), "decodificadores_invalido"),
    ("puente_clave", lambda v: str(v) if CLAVE_PUENTE.match(str(v)) else None, "clave_puente_invalida"),
    ("rx_fuente", lambda v: str(v) if len(str(v)) <= 200 and str(v).isprintable() else None, "fuente_ndi_invalida"),
    ("hdmi_consola", lambda v: v if v in MODOS_CONSOLA else None, "consola_invalida"),
    ("transporte", lambda v: v if v in ("tcp", "udp") else None, "transporte_invalido"),
    ("ap_ssid", lambda v: str(v).strip() if 1 <= len(str(v).strip().encode()) <= 32
     and str(v).strip().isprintable() else None, "ssid_invalido"),
    # la clave antigua por defecto es pública: no se admite ni a mano
    ("ap_clave", lambda v: str(v) if 8 <= len(str(v)) <= 63 and str(v).isprintable()
     and str(v) != AP_CLAVE_ANTIGUA else None, "clave_wifi_invalida"),
    ("idioma", lambda v: v if v in IDIOMAS else None, "idioma_invalido"),
    ("pais", lambda v: str(v).upper() if re.fullmatch(r"[A-Za-z]{2}", str(v)) else None, "pais_invalido"),
    ("cortafuegos", lambda v: bool(v), ""),
    ("simultaneo", lambda v: bool(v), ""),
    ("puente_ndi", lambda v: bool(v), ""),
    ("programa_audio", lambda v: str(v), ""),           # que exista lo mira el panel
    ("programa_retraso", lambda v: _entero(v, 0, RETRASO_MAX_MS), "retraso_invalido"),
    ("programa_grabar", lambda v: bool(v), ""),
    ("puente_hdmi", lambda v: str(v), ""),              # que exista lo mira el panel
    ("reem_fuente", lambda v: str(v).strip() if len(str(v)) <= 200 else None, "fuente_invalida"),
    ("reem_salidas", lambda v: _lista(v, SALIDAS_EXTRA), "salida_gpu_invalida"),
    ("reem_kbps", lambda v: _entero(v, 1000, 50000), "kbps_invalido"),
    ("reem_srt_puerto", lambda v: _entero(v, 1024, 65535), "puerto_invalido"),
    ("reem_srt_latencia", lambda v: _entero(v, 20, 2000), "srt_latencia_invalida"),
    ("reem_rtmp_url", _url_rtmp, "url_rtmp_invalida"),
    ("puertos_extra", lambda v: str(v).strip() if len(str(v)) <= 300
     and puertos_extra(str(v)) is not None else None, "puertos_invalidos"),
    ("motor", lambda v: v if v in ("modos", "rutas") else None, "motor_invalido"),
)

REGLAS_FUENTE = (
    ("activa", lambda v: bool(v), ""),
    ("ndi_nombre", lambda v: str(v).strip() if NOMBRE_NDI.match(str(v).strip()) else None, "nombre_ndi_invalido"),
    ("resolucion", lambda v: v if v in RESOLUCIONES else None, "resolucion_invalida"),
    ("fps", lambda v: v if v in FPS_VALIDOS else None, "fps_invalido"),
    ("audio", lambda v: str(v), ""),                    # que exista lo mira el panel
    ("gpu_salida", lambda v: str(v) if str(v) in SALIDAS_GPU else None, "salida_gpu_invalida"),
    ("gpu_extra", lambda v: _lista(v, SALIDAS_EXTRA), "salida_gpu_invalida"),
    ("gpu_kbps", lambda v: _entero(v, 1000, 40000), "kbps_invalido"),
    ("srt_puerto", lambda v: _entero(v, 1024, 65535), "puerto_invalido"),
    ("latencia", lambda v: v if v in LATENCIAS else None, "latencia_invalida"),
    ("solo_monitor", lambda v: bool(v), ""),
    ("retraso_audio", lambda v: _entero(v, 0, RETRASO_MAX_MS), "retraso_invalido"),
    ("srt_latencia", lambda v: _entero(v, 20, 2000), "srt_latencia_invalida"),
    ("formato", lambda v: v if v in FORMATOS else None, "formato_invalido"),
    ("grabar_cuando", lambda v: v if v in GRABAR_CUANDO else None, "grabar_cuando_invalido"),
    ("grabar_desde", lambda v: str(v) if HORA.match(str(v)) else None, "hora_invalida"),
    ("grabar_hasta", lambda v: str(v) if HORA.match(str(v)) else None, "hora_invalida"),
    ("grabar_manual", lambda v: bool(v), ""),
    ("rtmp_url", _url_rtmp, "url_rtmp_invalida"),
)


def _aplicar(reglas, d, previo):
    cambios, errores = {}, {}
    for campo, limpiar, codigo in reglas:
        if campo not in d:
            continue
        try:
            v = limpiar(d[campo])
        except (TypeError, ValueError, AttributeError):
            v = None
        if v is None:
            errores[campo] = codigo or "valor_invalido"
            continue
        if v == "oculta":
            # la URL con puntos del panel: se queda la guardada (si la hay)
            if previo and previo.get(campo):
                continue
            v = ""
        cambios[campo] = v
    return cambios, errores


def validar_config(d, previo=None):
    """Ajustes generales. 'previo': los actuales, para conservar la URL RTMP
    cuando el panel manda la versión oculta."""
    return _aplicar(REGLAS_CONFIG, d, previo)


def validar_fuente(d, previo=None):
    """Ajustes de una cámara."""
    return _aplicar(REGLAS_FUENTE, d, previo)


def validar_copia(cfg):
    """Una copia de seguridad entera: lo que no vale se descarta campo a campo
    (una copia no se rechaza por un valor roto; se restaura lo demás).
    Devuelve (cambios, campos_ignorados)."""
    cambios, errores = validar_config(cfg)
    ignorados = sorted(errores)
    if isinstance(cfg.get("clave_panel"), str) and (
            cfg["clave_panel"] == "" or RESUMEN_CLAVE.match(cfg["clave_panel"])):
        cambios["clave_panel"] = cfg["clave_panel"]
    elif "clave_panel" in cfg:
        ignorados.append("clave_panel")
    fuentes = cfg.get("fuentes")
    if isinstance(fuentes, dict):
        limpias = {}
        for ident, aj in fuentes.items():
            if not isinstance(aj, dict) or not re.fullmatch(r"[\w.:-]{1,80}", str(ident)):
                ignorados.append(f"fuentes.{ident}")
                continue
            c, e = validar_fuente(aj)
            ignorados += [f"fuentes.{ident}.{k}" for k in sorted(e)]
            limpias[str(ident)] = c
        cambios["fuentes"] = limpias
    elif "fuentes" in cfg:
        ignorados.append("fuentes")
    # las rutas y entradas de red las valida el motor al arrancar (rutas.validar),
    # con las entradas que haya en ese momento: aquí solo que sean listas
    for campo in ("entradas_red", "rutas"):
        if campo in cfg:
            if isinstance(cfg[campo], list) and all(isinstance(x, dict) for x in cfg[campo]):
                cambios[campo] = cfg[campo]
            else:
                ignorados.append(campo)
    return cambios, ignorados
