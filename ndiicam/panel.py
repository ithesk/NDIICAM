# NDIICAM — servidor web: panel y API.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
import base64
import datetime
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import nucleo
from nucleo import (AP_IP, CLAVE_PUENTE, ESTADO, EVENTOS, FPS_VALIDOS, GRABACIONES,
    GRABAR_CUANDO, HORA, IDIOMAS, MODOS, MODOS_CONSOLA, NOMBRE_EQUIPO, NOMBRE_GRABACION,
    NOMBRE_NDI, RESOLUCIONES, SALIDAS_GPU, URL_RTMP, WEB, clave_correcta, cliente_ip,
    configurar_ndi, evento, resumen_clave, sh, FORMATOS)
from camaras import fijar_modo_consola
from gpu import capacidad_gpu
from puente import clave_puente, xml_atem
from actualizador import (cambiar_nombre_equipo, reiniciar_servicio, respaldo, restaurar,
    valores_fabrica)
from estado import grabaciones


class Panel(BaseHTTPRequestHandler):
    server_version = "NDIICAM"

    def log_message(self, *args):
        pass

    def _json(self, datos, codigo=200):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _error(self, codigo_error, http=400):
        self._json({"error": codigo_error}, http)

    def _cuerpo(self, maximo=16384):
        n = min(int(self.headers.get("Content-Length") or 0), maximo)
        try:
            datos = json.loads(self.rfile.read(n) or b"{}")
            return datos if isinstance(datos, dict) else {}
        except ValueError:
            return {}

    def _desde_ap(self):
        return cliente_ip(self).startswith("10.42.0.")

    def _autorizado(self):
        """Contraseña opcional del panel. Desde la WiFi de configuración no se
        pide: quien está ahí ya conoce su clave, y así sirve para recuperarla."""
        guardada = nucleo.CONF.get()["clave_panel"]
        if not guardada or self._desde_ap():
            return True
        cab = self.headers.get("Authorization", "")
        if cab.startswith("Basic "):
            try:
                clave = base64.b64decode(cab[6:]).decode().partition(":")[2]
            except ValueError:
                return False
            return clave_correcta(clave, guardada)
        return False

    def _pedir_clave(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="NDIICAM"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirigir_portal(self):
        """Desde el AP, cualquier otro host (detección de portal de los móviles,
        webs) se manda al panel."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if self._desde_ap() and host not in (AP_IP, f"{socket.gethostname().lower()}.local"):
            self.send_response(302)
            self.send_header("Location", f"http://{AP_IP}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        return False

    def do_GET(self):
        if self._redirigir_portal():
            return
        if not self._autorizado():
            return self._pedir_clave()
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            try:
                with open(WEB, "rb") as f:
                    cuerpo = f.read()
            except OSError:
                return self.send_error(500)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
        elif url.path == "/api/estado":
            cfg = nucleo.CONF.get()
            self._json({**ESTADO, "config": {
                "transporte": cfg["transporte"], "ap_ssid": cfg["ap_ssid"],
                "ap_clave": cfg["ap_clave"], "pais": cfg["pais"], "idioma": cfg["idioma"],
                "clave_panel": bool(cfg["clave_panel"]), "decodificadores": cfg["decodificadores"]},
                "desde_ap": self._desde_ap(), "eventos": list(EVENTOS)[:40],
                "conexion_wifi": nucleo.RED.conexion})
        elif url.path == "/api/fuente/preview":
            ident = parse_qs(url.query).get("id", [""])[0]
            f = next((x for x in nucleo.FUENTES.lista() if x.cam["id"] == ident), None)
            ruta = (f.obj or {}).get("preview") if f else None
            try:
                # una imagen vieja es que la cámara ya no entrega: mejor nada
                if not ruta or time.time() - os.path.getmtime(ruta) > 10:
                    return self._error("no_encontrado", 404)
                with open(ruta, "rb") as fh:
                    cuerpo = fh.read()
            except OSError:
                return self._error("no_encontrado", 404)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
        elif url.path == "/api/puente/atem.xml":
            # la IP de verdad: el ATEM puede no resolver nombres .local
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
                    so.connect(("1.1.1.1", 80))
                    ip = so.getsockname()[0]
            except OSError:
                ip = (self.headers.get("Host") or "").split(":")[0]
            cuerpo = xml_atem(ip, clave_puente(nucleo.CONF)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="NDIICAM-{socket.gethostname()}.xml"')
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
        elif url.path == "/api/grabaciones":
            self._json(grabaciones())
        elif url.path.startswith("/grabaciones/"):
            self._descargar(url.path[len("/grabaciones/"):])
        elif url.path == "/api/wifi/redes":
            buscar = parse_qs(url.query).get("buscar") == ["1"]
            self._json({"redes": nucleo.RED.redes(buscar), "guardadas": nucleo.RED.guardadas()})
        elif url.path == "/api/actualizacion":
            self._json(nucleo.ACTUALIZADOR.comprobar() if parse_qs(url.query).get("comprobar")
                       else nucleo.ACTUALIZADOR.info)
        elif url.path == "/api/respaldo":
            cuerpo = json.dumps(respaldo(), indent=2, ensure_ascii=False).encode()
            nombre = f"ndiicam-{socket.gethostname()}-{datetime.date.today():%Y%m%d}.json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{nombre}"')
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
        else:
            self._error("no_encontrado", 404)

    def _descargar(self, nombre):
        if not NOMBRE_GRABACION.match(nombre):
            return self._error("no_encontrado", 404)
        ruta = os.path.join(GRABACIONES, nombre)
        try:
            f = open(ruta, "rb")
        except OSError:
            return self._error("no_encontrado", 404)
        with f:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Disposition", f'attachment; filename="{nombre}"')
            self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
            self.end_headers()
            try:
                shutil.copyfileobj(f, self.wfile, 1024 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_POST(self):
        if not self._autorizado():
            return self._pedir_clave()
        ruta = urlparse(self.path).path
        if ruta == "/api/respaldo":
            d = self._cuerpo(maximo=262144)
            if "config" not in d and "wifi" not in d:
                return self._error("respaldo_invalido")
            restaurar(d)
            reiniciar_servicio()
            return self._json({"ok": True, "reinicio": True})
        d = self._cuerpo()
        if ruta == "/api/config":
            return self._config(d)
        if ruta == "/api/fuente":
            return self._fuente(d)
        if ruta == "/api/grabaciones/borrar":
            nombre = str(d.get("nombre", ""))
            if not NOMBRE_GRABACION.match(nombre):
                return self._error("no_encontrado", 404)
            try:
                os.remove(os.path.join(GRABACIONES, nombre))
            except OSError:
                return self._error("no_encontrado", 404)
            evento("grabacion_borrada", nombre=nombre)
            return self._json({"ok": True})
        if ruta == "/api/reiniciar":
            nucleo.FUENTES.reiniciar_todo()
            evento("reinicio_manual", modo=nucleo.CONF.get()["modo"])
            return self._json({"ok": True})
        if ruta == "/api/fuente/grabar":
            return self._grabar(d)
        if ruta == "/api/fuente/reiniciar":
            return self._json({"ok": nucleo.FUENTES.reiniciar(str(d.get("id", "")))})
        if ruta == "/api/wifi/conectar":
            ssid, clave = str(d.get("ssid", "")), str(d.get("clave", ""))
            if not 1 <= len(ssid.encode()) <= 32:
                return self._error("ssid_invalido")
            if clave and not 8 <= len(clave) <= 63:
                return self._error("clave_wifi_invalida")
            if not nucleo.RED.conectar(ssid, clave):
                return self._error("conexion_en_curso", 409)
            return self._json({"ok": True})
        if ruta == "/api/wifi/olvidar":
            ok = nucleo.RED.olvidar(str(d.get("id", "")))
            if ok:
                evento("red_olvidada", ssid=str(d.get("ssid") or d.get("id")))
            return self._json({"ok": ok}, 200 if ok else 400)
        if ruta == "/api/red/ip":
            return self._ip(d)
        if ruta == "/api/ap":
            activar = bool(d.get("activar"))
            if not nucleo.RED.forzar_ap(10 if activar else 0):
                return self._error("ap_no_simultaneo", 409)
            evento("ap_manual_on" if activar else "ap_manual_off")
            return self._json({"ok": True})
        if ruta == "/api/actualizacion/instalar":
            if nucleo.ACTUALIZADOR.info["en_curso"]:
                return self._error("actualizacion_en_curso", 409)
            if not nucleo.ACTUALIZADOR.instalar():
                return self._error("sin_actualizacion")
            return self._json({"ok": True})
        if ruta == "/api/fabrica":
            if d.get("confirmar") != "FABRICA":
                return self._error("confirmacion")
            valores_fabrica()
            reiniciar_servicio()
            return self._json({"ok": True, "reinicio": True})
        if ruta == "/api/sistema/reiniciar":
            evento("reinicio_equipo")
            threading.Timer(1.5, sh, args=(["systemctl", "reboot"],)).start()
            return self._json({"ok": True})
        self._error("no_encontrado", 404)

    def _grabar(self, d):
        """Botón Grabar / Parar: grabación manual de una cámara. Si la cámara no
        estaba en grabación manual, la pone (eso reinicia su emisión unos 2 s)."""
        ident = str(d.get("id", ""))
        fuente = next((f for f in nucleo.FUENTES.lista() if f.cam["id"] == ident), None)
        if fuente is None:
            return self._error("camara_invalida")
        grabar = bool(d.get("grabar"))
        if grabar and not capacidad_gpu()["disponible"]:
            return self._error("gpu_no_disponible")
        antes = nucleo.CONF.fuente(ident)
        cambios = {"grabar_manual": grabar}
        if grabar and (antes["gpu_salida"] != "grabar" or antes["grabar_cuando"] != "manual"):
            cambios.update(gpu_salida="grabar", grabar_cuando="manual")
        reinicia = any(antes.get(k) != v for k, v in cambios.items() if k != "grabar_manual")
        nucleo.CONF.actualizar_fuente(ident, cambios)
        evento("grabacion_manual", fuente=fuente.cam["nombre"], grabar=grabar)
        self._json({"ok": True, "reinicia": reinicia})

    def _fuente(self, d):
        ident = str(d.get("id", ""))
        fuente = next((f for f in nucleo.FUENTES.lista() if f.cam["id"] == ident), None)
        if fuente is None:
            return self._error("camara_invalida")
        cambios = {}
        if "activa" in d:
            cambios["activa"] = bool(d["activa"])
        if "ndi_nombre" in d:
            v = str(d["ndi_nombre"]).strip()
            if not NOMBRE_NDI.match(v):
                return self._error("nombre_ndi_invalido")
            cambios["ndi_nombre"] = v
        if "resolucion" in d:
            if d["resolucion"] not in RESOLUCIONES:
                return self._error("resolucion_invalida")
            cambios["resolucion"] = d["resolucion"]
        if "fps" in d:
            if d["fps"] not in FPS_VALIDOS:
                return self._error("fps_invalido")
            cambios["fps"] = d["fps"]
        if "audio" in d:
            v = str(d["audio"])
            if v not in ("auto", "off") and v not in {a["id"] for a in nucleo.FUENTES.audio}:
                return self._error("audio_invalido")
            cambios["audio"] = v
        if "gpu_salida" in d:
            v = str(d["gpu_salida"])
            if v not in SALIDAS_GPU:
                return self._error("salida_gpu_invalida")
            if v != "no" and not capacidad_gpu()["disponible"]:
                return self._error("gpu_no_disponible")
            cambios["gpu_salida"] = v
        if "gpu_kbps" in d:
            try:
                v = int(d["gpu_kbps"])
            except (TypeError, ValueError):
                v = 0
            if not 1000 <= v <= 40000:
                return self._error("kbps_invalido")
            cambios["gpu_kbps"] = v
        if "srt_puerto" in d:
            try:
                v = int(d["srt_puerto"])
            except (TypeError, ValueError):
                v = 0
            if not 1024 <= v <= 65535:
                return self._error("puerto_invalido")
            cambios["srt_puerto"] = v
        if "formato" in d:
            if d["formato"] not in FORMATOS:
                return self._error("modo_invalido")
            cambios["formato"] = d["formato"]
        if "grabar_cuando" in d:
            if d["grabar_cuando"] not in GRABAR_CUANDO:
                return self._error("modo_invalido")
            cambios["grabar_cuando"] = d["grabar_cuando"]
        for campo in ("grabar_desde", "grabar_hasta"):
            if campo in d:
                v = str(d[campo])
                if not HORA.match(v):
                    return self._error("hora_invalida")
                cambios[campo] = v
        if "rtmp_url" in d:
            v = str(d["rtmp_url"]).strip()
            if "•" not in v:                # con puntos es la que ya había, oculta
                if v and not URL_RTMP.match(v):
                    return self._error("url_rtmp_invalida")
                cambios["rtmp_url"] = v
        if cambios.get("gpu_salida") == "rtmp" and not (
                cambios.get("rtmp_url") or nucleo.CONF.fuente(ident)["rtmp_url"]):
            return self._error("url_rtmp_invalida")
        antes = nucleo.CONF.fuente(ident)
        cambios = {k: v for k, v in cambios.items() if antes.get(k) != v}
        if cambios:
            nucleo.CONF.actualizar_fuente(ident, cambios)
            nucleo.FUENTES.reiniciar(ident)
            evento("ajustes_fuente", fuente=fuente.cam["nombre"], campos=sorted(cambios))
        self._json({"ok": True})

    def _config(self, d):
        antes = nucleo.CONF.get()
        cambios, reiniciar = {}, False
        if "hdmi" in d:
            cambios["hdmi"] = bool(d["hdmi"])
        if "hdmi_fuente" in d:
            v = str(d["hdmi_fuente"])
            if v != "auto" and v not in {f.cam["id"] for f in nucleo.FUENTES.lista()}:
                return self._error("camara_invalida")
            cambios["hdmi_fuente"] = v
        if "modo" in d:
            if d["modo"] not in MODOS:
                return self._error("modo_invalido")
            cambios["modo"] = d["modo"]
        if "decodificadores" in d:
            v = str(d["decodificadores"])
            if v != "auto" and v not in {str(i) for i in range(1, 5)}:
                return self._error("modo_invalido")
            cambios["decodificadores"] = v if v == "auto" else int(v)
        if d.get("puente_clave_nueva"):
            # clave nueva: el ATEM tendrá que cargar los ajustes otra vez
            cambios["puente_clave"] = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
        if "puente_clave" in d:
            v = str(d["puente_clave"])
            if not CLAVE_PUENTE.match(v):
                return self._error("clave_puente_invalida")
            cambios["puente_clave"] = v
        if "rx_fuente" in d:
            # una fuente de la red: vale aunque ahora no se vea (se reintenta)
            v = str(d["rx_fuente"])
            if len(v) > 200 or not v.isprintable():
                return self._error("fuente_ndi_invalida")
            cambios["rx_fuente"] = v
        if "hdmi_consola" in d:
            if d["hdmi_consola"] not in MODOS_CONSOLA:
                return self._error("modo_invalido")
            if d["hdmi_consola"] != antes["hdmi_consola"]:
                if not fijar_modo_consola(d["hdmi_consola"]):
                    return self._error("sin_grub")
                cambios["hdmi_consola"] = d["hdmi_consola"]
        if "transporte" in d:
            if d["transporte"] not in ("tcp", "udp"):
                return self._error("transporte_invalido")
            cambios["transporte"] = d["transporte"]
        if "ap_ssid" in d:
            v = str(d["ap_ssid"]).strip()
            if not 1 <= len(v.encode()) <= 32 or not v.isprintable():
                return self._error("ssid_invalido")
            cambios["ap_ssid"] = v
        if "ap_clave" in d:
            v = str(d["ap_clave"])
            if not 8 <= len(v) <= 63 or not v.isprintable():
                return self._error("clave_wifi_invalida")
            cambios["ap_clave"] = v
        if "idioma" in d:
            if d["idioma"] not in IDIOMAS:
                return self._error("modo_invalido")
            cambios["idioma"] = d["idioma"]
        if "pais" in d:
            v = str(d["pais"]).upper()
            if not re.fullmatch(r"[A-Z]{2}", v):
                return self._error("pais_invalido")
            cambios["pais"] = v
        if "clave_panel" in d:
            v = str(d["clave_panel"])
            if v and len(v) < 4:
                return self._error("clave_panel_corta")
            cambios["clave_panel"] = resumen_clave(v) if v else ""
        nuevo_nombre = None
        if "nombre_equipo" in d:
            v = str(d["nombre_equipo"]).strip()
            if not NOMBRE_EQUIPO.match(v):
                return self._error("nombre_equipo_invalido")
            if v != socket.gethostname():
                nuevo_nombre = v
        cambios = {k: v for k, v in cambios.items() if antes.get(k) != v}
        if cambios:
            nucleo.CONF.actualizar(cambios)
            if "modo" in cambios:
                evento("modo", modo=cambios["modo"])
            if cambios.keys() - {"modo"}:
                evento("ajustes", campos=sorted(cambios.keys() - {"modo"}))
        if {"hdmi", "hdmi_fuente"} & cambios.keys():
            for f in nucleo.FUENTES.lista():
                f.hdmi_fallido = False      # volver a intentarlo con el ajuste nuevo
        if "decodificadores" in cambios:
            for f in nucleo.FUENTES.lista():
                f.hilos, f.retrasos = 1, 0  # el automático vuelve a empezar desde uno
        if "transporte" in cambios:
            configurar_ndi(cambios["transporte"])
            reiniciar = True    # el SDK solo lee su configuración al arrancar
        if nuevo_nombre:
            cambiar_nombre_equipo(nuevo_nombre)
            evento("nombre_equipo", nombre=nuevo_nombre)
            reiniciar = True    # el prefijo del nombre NDI sale del hostname
        if {"ap_ssid", "ap_clave", "pais"} & cambios.keys() and nucleo.RED.ap_activo:
            nucleo.RED.detener_ap()    # el bucle de red la levanta con los datos nuevos
        if reiniciar:
            reiniciar_servicio()
        self._json({"ok": True, "reinicio": reiniciar})

    def _ip(self, d):
        metodo = d.get("metodo")
        if metodo not in ("auto", "manual"):
            return self._error("ip_invalida")
        nuevo = {"metodo": metodo, "direccion": "", "prefijo": 24, "gateway": "", "dns": []}
        if metodo == "manual":
            try:
                red = ipaddress.ip_interface(f"{d.get('direccion')}/{int(d.get('prefijo', 24))}")
                gw = ipaddress.ip_address(str(d.get("gateway", "")))
                dns = [str(ipaddress.ip_address(x.strip()))
                       for x in re.split(r"[,\s]+", str(d.get("dns", ""))) if x.strip()]
            except ValueError:
                return self._error("ip_invalida")
            if red.network.prefixlen > 30 or gw not in red.network or gw == red.ip:
                return self._error("gateway_invalido")
            nuevo.update(direccion=str(red.ip), prefijo=red.network.prefixlen,
                         gateway=str(gw), dns=dns or [str(gw)])
        conexion = d.get("conexion", "activa")
        if conexion not in ("activa", "cable"):
            return self._error("ip_invalida")
        if not nucleo.RED.cambiar_ip(nuevo, conexion):
            return self._error("cambio_ip_en_curso", 409)
        self._json({"ok": True})

class Servidor(ThreadingHTTPServer):
    """IPv4 e IPv6 en el mismo socket (<equipo>.local resuelve a ambos)."""
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()
