# NDIICAM — servidor web: panel y API.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
import base64
import datetime
import gzip
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import nucleo
from nucleo import (AP_CLAVE_DEFECTO, AP_IP, ESTADO, EVENTOS, GRABACIONES, LIMITE_ENTRADAS,
    LIMITE_RUTAS, NOMBRE_EQUIPO, NOMBRE_GRABACION, SinEspacio, WEB, clave_correcta,
    cliente_ip, configurar_ndi, evento, resumen_clave, sh)
from validar import validar_config, validar_fuente
from camaras import fijar_modo_consola
from gpu import capacidad_gpu, simultaneo
from puente import clave_puente, xml_atem
from supervisor import ocultar_clave_rtmp
from camaras import camaras as camaras_conectadas
from estado import estado_rutas
import rutas as rutas_mod
from actualizador import (cambiar_nombre_equipo, reiniciar_servicio, respaldo, restaurar,
    valores_fabrica)
from estado import grabaciones
from seguridad import CLAVE_MINIMA, COOKIE, DURACION_SESION, huella_certificado
import sistema

CUERPO_MAX = 16384              # bytes de cuerpo de una petición normal
CUERPO_MAX_GRANDE = 262144      # rutas enteras y copias de seguridad
CONEXIONES_MAX = 32             # peticiones atendidas a la vez
RECUPERACION_AP = 600           # s tras levantar la WiFi de configuración sin pedir contraseña
_AUTH_CACHE = {}                # (ip, sha256(clave)) -> hasta cuándo vale, para Basic Auth
_auth_lock = threading.Lock()


class Pagina:
    """index.html listo para servir: comprimido, con ETag y con los hashes de
    sus <script> para la Content-Security-Policy. Se relee si cambia en disco."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mtime = None
        self.cuerpo = self.gz = self.etag = self.csp = None

    def cargar(self):
        try:
            mtime = os.path.getmtime(WEB)
        except OSError:
            return None
        with self.lock:
            if mtime != self.mtime:
                with open(WEB, "rb") as f:
                    self.cuerpo = f.read()
                self.gz = gzip.compress(self.cuerpo, 6)
                self.etag = '"' + hashlib.sha256(self.cuerpo).hexdigest()[:32] + '"'
                hashes = " ".join(
                    "'sha256-" + base64.b64encode(hashlib.sha256(m.group(1).encode()).digest()).decode() + "'"
                    for m in re.finditer(r"<script>(.*?)</script>", self.cuerpo.decode(), re.S))
                # scripts: solo los inline de la propia página (por hash); estilos inline
                # (hay atributos style); imágenes del propio equipo; nada de otros sitios
                scripts = hashes or "'self'"
                self.csp = (f"default-src 'self'; script-src {scripts}; "
                            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                            "form-action 'self'")
                self.mtime = mtime
            return self


PAGINA = Pagina()


class Panel(BaseHTTPRequestHandler):
    server_version = "NDIICAM"
    # leer cabeceras y cuerpo: un cliente que no habla no retiene el hilo. Las
    # descargas largas lo suben a mano
    timeout = 10

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

    def _cuerpo(self, maximo=CUERPO_MAX):
        """El JSON del cuerpo, o None tras responder el error: un Content-Length
        negativo o absurdo no hace leer hasta el infinito, y uno mayor que el
        máximo se rechaza (antes se truncaba y se daba por bueno)."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > maximo:
            self._error("cuerpo_grande", 413)
            return None
        try:
            datos = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            self._error("json_invalido")
            return None
        return datos if isinstance(datos, dict) else {}

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        if PAGINA.csp:
            self.send_header("Content-Security-Policy", PAGINA.csp)
        super().end_headers()

    def _desde_ap(self):
        return cliente_ip(self).startswith("10.42.0.")

    def _recuperacion_ap(self):
        """Desde la WiFi de configuración se entra sin contraseña solo mientras no
        exista (primer uso) o en los 10 minutos siguientes a levantarla: así sigue
        sirviendo para recuperar una contraseña olvidada (basta reiniciar el
        equipo sin red), pero no da el panel a cualquiera que sepa la clave WiFi."""
        if not self._desde_ap():
            return False
        if not nucleo.CONF.get()["clave_panel"]:
            return True
        red = nucleo.RED
        return bool(red and red.ap_activo and time.time() - red.ap_desde < RECUPERACION_AP)

    def _testigo(self):
        try:
            c = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        return c[COOKIE].value if COOKIE in c else None

    def _autorizado(self):
        """La contraseña del panel es obligatoria. Desde la WiFi de configuración
        no se pide: quien está ahí ya conoce su clave, y así sirve para recuperarla.
        Vale la cookie de sesión (el navegador) o Basic Auth (scripts, Companion)."""
        if self._recuperacion_ap():
            return True
        guardada = nucleo.CONF.get()["clave_panel"]
        if not guardada:
            return False        # primero hay que crearla
        if nucleo.SESIONES.valida(self._testigo()):
            return True
        cab = self.headers.get("Authorization", "")
        if cab.startswith("Basic "):
            ip = cliente_ip(self)
            if nucleo.INTENTOS.espera(ip):
                return False
            try:
                clave = base64.b64decode(cab[6:]).decode().partition(":")[2]
            except ValueError:
                clave = ""
            # Companion pide el estado cada segundo: no recalcular PBKDF2 cada vez
            marca = (ip, hashlib.sha256(clave.encode()).hexdigest(), guardada)
            ahora = time.monotonic()
            with _auth_lock:
                if _AUTH_CACHE.get(marca, 0) > ahora:
                    return True
            if clave_correcta(clave, guardada):
                nucleo.INTENTOS.acierto(ip)
                with _auth_lock:
                    if len(_AUTH_CACHE) > 200:
                        _AUTH_CACHE.clear()
                    _AUTH_CACHE[marca] = ahora + 60
                return True
            nucleo.INTENTOS.fallo(ip)
        return False

    def _pedir_clave(self):
        self._error("sin_sesion", 401)

    def _cookie(self, testigo, edad=DURACION_SESION):
        # Lax: al abrir el panel desde el QR del HDMI o un enlace llega la cookie y no
        # pide entrar otra vez; los POST siguen protegidos por la comprobación de Origin
        partes = [f"{COOKIE}={testigo}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={edad}"]
        if getattr(self.server, "tls", False):
            partes.append("Secure")
        return "; ".join(partes)

    def _json_sesion(self, datos, testigo, edad=DURACION_SESION):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", self._cookie(testigo, edad))
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _mismo_origen(self):
        """Contra peticiones falsificadas desde otra web (además de SameSite)."""
        origen = self.headers.get("Origin")
        if not origen or origen == "null":
            return origen is None
        return urlparse(origen).netloc.lower() == (self.headers.get("Host") or "").lower()

    def _a_https(self):
        """Por la red, el panel va cifrado: HTTP solo redirige. Desde la WiFi de
        configuración sigue en HTTP (el portal cautivo de los móviles no acepta
        un certificado propio)."""
        if getattr(self.server, "tls", False) or self._desde_ap() or not nucleo.HTTPS:
            return False
        host = (self.headers.get("Host") or socket.gethostname() + ".local").rsplit("]", 1)
        host = host[0] + "]" if len(host) == 2 else host[0].split(":")[0]
        self.send_response(301)
        self.send_header("Location", f"https://{host}{self.path}")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _sesion_info(self):
        cfg = nucleo.CONF.get()
        return {"autenticado": self._autorizado(), "clave_creada": bool(cfg["clave_panel"]),
                "desde_ap": self._desde_ap(), "recuperacion": self._recuperacion_ap(),
                "espera": nucleo.INTENTOS.espera(cliente_ip(self)),
                "https": bool(getattr(self.server, "tls", False)), "minimo": CLAVE_MINIMA,
                "idioma": cfg["idioma"]}

    def _entrar(self, d):
        ip = cliente_ip(self)
        espera = nucleo.INTENTOS.espera(ip)
        if espera:
            return self._json({"error": "demasiados_intentos", "espera": espera}, 429)
        guardada = nucleo.CONF.get()["clave_panel"]
        if not guardada or not clave_correcta(str(d.get("clave", "")), guardada):
            nucleo.INTENTOS.fallo(ip)
            evento("acceso_fallido", ip=ip)
            espera = nucleo.INTENTOS.espera(ip)
            return self._json({"error": "clave_incorrecta", "espera": espera}, 429 if espera else 403)
        nucleo.INTENTOS.acierto(ip)
        evento("acceso", ip=ip)
        self._json_sesion({"ok": True}, nucleo.SESIONES.crear(ip))

    def _crear_clave(self, d):
        """Primera vez: se elige la contraseña (no hay ninguna por defecto)."""
        if nucleo.CONF.get()["clave_panel"]:
            return self._error("clave_ya_creada", 409)
        v = str(d.get("clave", ""))
        if len(v) < CLAVE_MINIMA or len(v) > 128:
            return self._error("clave_panel_corta")
        nucleo.CONF.actualizar({"clave_panel": resumen_clave(v)})
        evento("clave_creada", ip=cliente_ip(self))
        self._json_sesion({"ok": True}, nucleo.SESIONES.crear(cliente_ip(self)))

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
        if self._redirigir_portal() or self._a_https():
            return
        url = urlparse(self.path)
        if url.path == "/api/sesion":
            return self._json(self._sesion_info())
        # la página es pública (no lleva datos): sin sesión, enseña la entrada
        if url.path not in ("/", "/index.html") and not self._autorizado():
            return self._pedir_clave()
        if url.path in ("/", "/index.html"):
            pg = PAGINA.cargar()
            if pg is None:
                return self.send_error(500)
            # 250 KB sin comprimir: por la WiFi de configuración son segundos.
            # Comprimida y con ETag, las recargas no vuelven a bajarla
            if self.headers.get("If-None-Match") == pg.etag:
                self.send_response(304)
                self.send_header("ETag", pg.etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            comprimir = "gzip" in (self.headers.get("Accept-Encoding") or "")
            cuerpo = pg.gz if comprimir else pg.cuerpo
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("ETag", pg.etag)
            self.send_header("Vary", "Accept-Encoding")
            if comprimir:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
        elif url.path == "/api/estado":
            cfg = nucleo.CONF.get()
            pg = PAGINA.cargar()
            # la huella del panel que se sirve ahora: si no es la de la pestaña
            # abierta (se actualizó el equipo), el panel pide recargar
            self._json({**ESTADO, "panel": pg.etag if pg else None, "config": {
                "transporte": cfg["transporte"], "ap_ssid": cfg["ap_ssid"],
                "ap_clave": cfg["ap_clave"], "pais": cfg["pais"], "idioma": cfg["idioma"],
                "clave_panel": bool(cfg["clave_panel"]), "decodificadores": cfg["decodificadores"],
                "cortafuegos": cfg["cortafuegos"], "puertos_extra": cfg["puertos_extra"],
                "simultaneo": cfg["simultaneo"], "motor": cfg.get("motor", "modos"),
                **{k: cfg[k] for k in ("programa_audio", "programa_retraso", "programa_grabar",
                                       "puente_hdmi")},
                **{k: cfg[k] for k in ("reem_fuente", "reem_salidas", "reem_kbps", "reem_srt_puerto",
                                       "reem_srt_latencia")},
                "reem_rtmp_url": ocultar_clave_rtmp(cfg["reem_rtmp_url"])},
                "desde_ap": self._desde_ap(), "eventos": list(EVENTOS)[:40],
                "conexion_wifi": nucleo.RED.conexion,
                "seguridad": {"https": bool(getattr(self.server, "tls", False)),
                              "https_disponible": bool(nucleo.HTTPS),
                              "huella": huella_certificado() if nucleo.HTTPS else None,
                              "ap_clave_defecto": cfg["ap_clave"] == AP_CLAVE_DEFECTO,
                              "cortafuegos": nucleo.CORTAFUEGOS.estado(cfg)}})
        elif url.path == "/api/fuente/preview":
            ident = parse_qs(url.query).get("id", [""])[0]
            f = next((x for x in nucleo.FUENTES.lista() if x.cam["id"] == ident), None)
            if f is None:       # con rutas, la cámara corre dentro de su ruta
                f = next((x for x in nucleo.FUENTES.motor.procs.values()
                          if getattr(x, "cam", {}).get("id") == ident), None)
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
        elif url.path == "/api/rutas":
            nucleo.PANEL_VISTO = time.time()    # alguien mira: vista previa de las cámaras libres
            self._json(estado_rutas())
        elif url.path == "/api/puente/atem.xml":
            # la IP de verdad: el ATEM puede no resolver nombres .local
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
                    so.connect(("1.1.1.1", 80))
                    ip = so.getsockname()[0]
            except OSError:
                ip = (self.headers.get("Host") or "").split(":")[0]
            # con rutas, la clave de esa entrada; si no, la del modo puente
            ent = parse_qs(url.query).get("entrada", [""])[0]
            clave = next((e.get("clave") for e in nucleo.CONF.get().get("entradas_red") or []
                          if e.get("id") == ent and e.get("tipo") == "ingesta"), None)
            cuerpo = xml_atem(ip, clave or clave_puente(nucleo.CONF)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="NDIICAM-{socket.gethostname()}.xml"')
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
        elif url.path == "/api/grabaciones":
            self._json(grabaciones())
        elif url.path == "/api/registro":
            self._registro(parse_qs(url.query))
        elif url.path == "/api/registro/arranques":
            self._json({"arranques": sistema.arranques()})
        elif url.path == "/api/sistema/zonas":
            self._json({"zonas": sistema.zonas(), **sistema.hora()})
        elif url.path == "/api/respaldo":
            # la copia lleva las claves WiFi: se pide por POST con la contraseña
            self._error("usar_post", 405)
        elif url.path.startswith("/grabaciones/"):
            self._descargar(url.path[len("/grabaciones/"):])
        elif url.path == "/api/wifi/redes":
            buscar = parse_qs(url.query).get("buscar") == ["1"]
            self._json({"redes": nucleo.RED.redes(buscar), "guardadas": nucleo.RED.guardadas()})
        elif url.path == "/api/actualizacion":
            self._json(nucleo.ACTUALIZADOR.comprobar() if parse_qs(url.query).get("comprobar")
                       else nucleo.ACTUALIZADOR.info)
        else:
            self._error("no_encontrado", 404)

    def _descargar_respaldo(self, d):
        """La copia de seguridad lleva la configuración entera y las claves de las
        WiFi guardadas: además de la sesión, hace falta la contraseña del panel."""
        if not self._clave_actual(d):
            return self._error("clave_actual_incorrecta", 403)
        cuerpo = json.dumps(respaldo(), indent=2, ensure_ascii=False).encode()
        nombre = f"ndiicam-{socket.gethostname()}-{datetime.date.today():%Y%m%d}.json"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{nombre}"')
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _clave_actual(self, d):
        """Si el cuerpo trae la contraseña del panel correcta (para lo delicado:
        cambiarla, descargar la copia). En la ventana de recuperación desde la
        WiFi de configuración no hace falta."""
        if self._recuperacion_ap():
            return True
        guardada = nucleo.CONF.get()["clave_panel"]
        if guardada and clave_correcta(str(d.get("clave_actual", "")), guardada):
            return True
        nucleo.INTENTOS.fallo(cliente_ip(self))
        return False

    def _descargar(self, nombre):
        if not NOMBRE_GRABACION.match(nombre):
            return self._error("no_encontrado", 404)
        ruta = os.path.join(GRABACIONES, nombre)
        try:
            f = open(ruta, "rb")
        except OSError:
            return self._error("no_encontrado", 404)
        with f:
            self.connection.settimeout(60)     # por WiFi floja un trozo puede tardar
            self.send_response(200)
            self.send_header("Content-Type", "video/x-matroska" if nombre.endswith(".mkv") else "video/mp4")
            self.send_header("Content-Disposition", f'attachment; filename="{nombre}"')
            self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
            self.end_headers()
            try:
                shutil.copyfileobj(f, self.wfile, 1024 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _registro(self, q):
        uno = lambda k, defecto="": q.get(k, [defecto])[0]   # noqa: E731
        try:
            n = int(uno("n", "300"))
        except ValueError:
            n = 300
        lineas = sistema.registro(uno("origen", "servicio"), uno("arranque"), uno("nivel", "todo"),
                                  uno("buscar"), n)
        if lineas is None:
            return self._error("registro_invalido")
        if uno("formato") != "txt":
            return self._json({"lineas": lineas})
        cuerpo = sistema.registro_texto(lineas).encode()
        nombre = f"ndiicam-{socket.gethostname()}-{uno('origen', 'servicio')}-{datetime.date.today():%Y%m%d}.log"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{nombre}"')
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_POST(self):
        if not self._mismo_origen():
            return self._error("origen", 403)
        ruta = urlparse(self.path).path
        if not getattr(self.server, "tls", False) and nucleo.HTTPS and not self._desde_ap():
            return self._error("usar_https", 403)    # nada viaja sin cifrar por la red
        grande = ruta in ("/api/respaldo", "/api/rutas")
        d = self._cuerpo(CUERPO_MAX_GRANDE if grande else CUERPO_MAX)
        if d is None:
            return
        try:
            self._post(ruta, d)
        except SinEspacio:
            # el cambio se aplicó en memoria pero no cabe en el disco
            self._error("sin_espacio", 507)

    def _post(self, ruta, d):
        if ruta == "/api/login":
            return self._entrar(d)
        if ruta == "/api/clave/crear":
            return self._crear_clave(d)
        if ruta == "/api/logout":
            nucleo.SESIONES.cerrar(self._testigo())
            return self._json_sesion({"ok": True}, "", 0)
        if not self._autorizado():
            return self._pedir_clave()
        if ruta == "/api/respaldo":
            if "config" not in d and "wifi" not in d:
                return self._error("respaldo_invalido")
            restaurar(d)
            reiniciar_servicio()
            return self._json({"ok": True, "reinicio": True})
        if ruta == "/api/respaldo/descargar":
            return self._descargar_respaldo(d)
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
        if ruta == "/api/rutas":
            return self._guardar_rutas(d)
        if ruta == "/api/rutas/motor":
            return self._motor(d)
        if ruta == "/api/rutas/vivo":
            # ganancia, retraso y silencio de una ruta: se aplican en marcha, sin
            # esperar a que el panel aplique el resto de cambios (que reinician cosas)
            cfg = nucleo.CONF.get()
            rutas = [dict(r) for r in cfg.get("rutas") or []]
            r = next((x for x in rutas if x.get("id") == str(d.get("ruta", ""))), None)
            if r is None:
                return self._error("ruta_invalida")
            try:
                if "ganancia" in d:
                    r["ganancia"] = max(rutas_mod.GANANCIA[0], min(rutas_mod.GANANCIA[1], int(d["ganancia"])))
                if "retraso" in d:
                    r["retraso"] = max(0, min(rutas_mod.RETRASO_MAX, int(d["retraso"])))
            except (TypeError, ValueError):
                return self._error("valor_invalido")
            if "mudo" in d:
                r["mudo"] = bool(d["mudo"])
            nucleo.CONF.actualizar({"rutas": rutas})
            return self._json({"ok": True})
        if ruta == "/api/rutas/calibrar":
            if not nucleo.FUENTES.motor.calibrar(str(d.get("ruta", ""))):
                return self._error("sin_audio")
            return self._json({"ok": True, "segundos": 8})
        if ruta == "/api/fuente/calibrar":
            # unas palmadas delante de la cámara miden el desfase entre audio e imagen
            if not nucleo.FUENTES.calibrar(str(d.get("id", ""))):
                return self._error("sin_audio")
            return self._json({"ok": True, "segundos": 8})
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
            sistema.marcar_fin("reinicio_panel")
            threading.Timer(1.5, sh, args=(["systemctl", "reboot"],)).start()
            return self._json({"ok": True})
        if ruta == "/api/sistema/apagar":
            evento("apagado_equipo")
            sistema.marcar_fin("apagado_panel")
            threading.Timer(1.5, sh, args=(["systemctl", "poweroff"],)).start()
            return self._json({"ok": True})
        if ruta == "/api/sistema/zona":
            zona = str(d.get("zona", ""))
            if not sistema.cambiar_zona(zona):
                return self._error("zona_invalida")
            evento("zona_horaria", zona=zona)
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
        if grabar and antes["gpu_salida"] in ("srt", "rtmp") and simultaneo(nucleo.CONF.get()):
            # equipo potente: graba además de emitir, sin cortar la emisión SRT/RTMP
            if "grabar" not in antes["gpu_extra"] or antes["grabar_cuando"] != "manual":
                cambios.update(gpu_extra=sorted({*antes["gpu_extra"], "grabar"}),
                               grabar_cuando="manual")
        elif grabar and (antes["gpu_salida"] != "grabar" or antes["grabar_cuando"] != "manual"):
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
        antes = nucleo.CONF.fuente(ident)
        # las reglas de cada campo están en validar.py (las mismas que al restaurar
        # una copia); aquí solo lo que depende del equipo
        cambios, errores = validar_fuente(d, antes)
        if errores:
            return self._error(next(iter(errores.values())))
        if "audio" in cambios and cambios["audio"] not in ("auto", "off") and \
                cambios["audio"] not in {a["id"] for a in nucleo.FUENTES.audio}:
            return self._error("audio_invalido")
        if cambios.get("gpu_salida", "no") != "no" and not capacidad_gpu()["disponible"]:
            return self._error("gpu_no_disponible")
        quiere_rtmp = (cambios.get("gpu_salida") == "rtmp"
                       or "rtmp" in cambios.get("gpu_extra", []))
        if quiere_rtmp and not (cambios.get("rtmp_url") or antes["rtmp_url"]):
            return self._error("url_rtmp_invalida")
        cambios = {k: v for k, v in cambios.items() if antes.get(k) != v}
        if cambios:
            nucleo.CONF.actualizar_fuente(ident, cambios)
            if cambios.keys() != {"retraso_audio"}:     # el retraso se ajusta en marcha
                nucleo.FUENTES.reiniciar(ident)
            evento("ajustes_fuente", fuente=fuente.cam["nombre"], campos=sorted(cambios))
        self._json({"ok": True})

    def _guardar_rutas(self, d):
        """El montaje entero desde el panel: entradas de red y rutas. Lo que no
        cuadra se guarda igual y la ruta queda parada con su motivo."""
        cfg = nucleo.CONF.get()
        cambios = {}
        if "entradas_red" in d:
            if not isinstance(d["entradas_red"], list) or len(d["entradas_red"]) > LIMITE_ENTRADAS \
                    or not all(isinstance(e, dict) for e in d["entradas_red"]):
                return self._json({"error": "entradas_invalidas", "maximo": LIMITE_ENTRADAS}, 400)
            red, ids = [], set()
            antes = {e.get("id"): e for e in cfg.get("entradas_red") or []}
            for e in d["entradas_red"]:
                tipo, eid = str(e.get("tipo")), str(e.get("id") or "")[:40]
                if tipo not in ("ndi", "ingesta") or not re.fullmatch(r"[\w-]{1,40}", eid) or eid in ids:
                    return self._error("entradas_invalidas")
                ids.add(eid)
                ne = {"id": eid, "tipo": tipo, "nombre": str(e.get("nombre") or tipo)[:60]}
                if tipo == "ndi":
                    ne["fuente"] = str(e.get("fuente") or "")[:200]
                else:
                    # la clave no la elige el panel: se genera una vez y se conserva
                    ne["clave"] = (antes.get(eid) or {}).get("clave") or \
                        secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
                red.append(ne)
            cambios["entradas_red"] = red
        if "rutas" in d:
            if not isinstance(d["rutas"], list) or len(d["rutas"]) > LIMITE_RUTAS or \
                    not all(isinstance(r, dict) for r in d["rutas"]):
                return self._json({"error": "rutas_invalidas", "maximo": LIMITE_RUTAS}, 400)
            # el panel recibe las URL RTMP con la clave tapada: si la devuelve así,
            # se queda la que había
            previas = {s.get("id"): s.get("url") for r in cfg.get("rutas") or []
                       for s in r.get("salidas") or [] if s.get("tipo") == "rtmp"}
            for r in d["rutas"]:
                for sal in (r.get("salidas") if isinstance(r.get("salidas"), list) else []):
                    if isinstance(sal, dict) and sal.get("tipo") == "rtmp" and "•" in str(sal.get("url") or ""):
                        sal["url"] = previas.get(sal.get("id")) or ""
            ents = nucleo.FUENTES.motor.entradas_actuales(
                {c["id"]: c for c in camaras_conectadas()}, {**cfg, **cambios})
            try:
                limpias, _ = rutas_mod.validar(d["rutas"], ents)
            except (TypeError, ValueError, AttributeError, KeyError):
                return self._json({"error": "rutas_invalidas", "maximo": LIMITE_RUTAS}, 400)
            cambios["rutas"] = limpias
        if cambios:
            nucleo.CONF.actualizar(cambios)
            evento("rutas_guardadas", rutas=len(cambios.get("rutas", cfg.get("rutas") or [])))
        return self._json({"ok": True, **estado_rutas()})

    def _motor(self, d):
        """Pasar al motor de rutas (la primera vez, con las rutas equivalentes a los
        modos) o volver a los modos."""
        motor = str(d.get("motor"))
        if motor not in ("rutas", "modos"):
            return self._error("motor_invalido")
        cfg = nucleo.CONF.get()
        cambios = {"motor": motor}
        if motor == "rutas" and not cfg.get("rutas"):
            cams = {c["id"]: c for c in camaras_conectadas()}
            red, lista = rutas_mod.desde_modos(cfg, cams, nucleo.CONF.fuente,
                                               [a["id"] for a in nucleo.FUENTES.audio])
            ents = nucleo.FUENTES.motor.entradas_actuales(cams, {**cfg, "entradas_red": red})
            cambios.update(entradas_red=red, rutas=rutas_mod.validar(lista, ents)[0])
        nucleo.CONF.actualizar(cambios)
        evento("motor", motor=motor)
        return self._json({"ok": True, **estado_rutas()})

    def _config(self, d):
        antes = nucleo.CONF.get()
        # las reglas de cada campo están en validar.py (las mismas que al restaurar
        # una copia); aquí solo lo que depende del equipo o de la sesión
        cambios, errores = validar_config(d, antes)
        if errores:
            return self._error(next(iter(errores.values())))
        reiniciar = False
        # solo cuando hace falta: lo que hay conectado ahora mismo
        camaras = lambda: {f.cam["id"] for f in nucleo.FUENTES.lista()}   # noqa: E731
        audios = lambda: {a["id"] for a in nucleo.FUENTES.audio}           # noqa: E731
        if "hdmi_fuente" in d:
            v = str(d["hdmi_fuente"])
            if v != "auto" and v not in camaras():
                return self._error("camara_invalida")
            cambios["hdmi_fuente"] = v
        if d.get("puente_clave_nueva"):
            # clave nueva: el ATEM tendrá que cargar los ajustes otra vez
            cambios["puente_clave"] = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
        if "hdmi_consola" in cambios:
            if cambios["hdmi_consola"] != antes["hdmi_consola"]:
                if not fijar_modo_consola(cambios["hdmi_consola"]):
                    return self._error("sin_grub")
            else:
                del cambios["hdmi_consola"]
        if "clave_panel" in d:
            # obligatoria: se cambia, no se quita. Hace falta la actual, salvo en la
            # ventana de recuperación desde la WiFi de configuración
            v = str(d["clave_panel"])
            if len(v) < CLAVE_MINIMA or len(v) > 128:
                return self._error("clave_panel_corta")
            if antes["clave_panel"] and not self._clave_actual(d):
                return self._error("clave_actual_incorrecta", 403)
            cambios["clave_panel"] = resumen_clave(v)
        if cambios.get("programa_audio") and cambios["programa_audio"] not in audios():
            return self._error("audio_invalido")
        if "puente_hdmi" in cambios and cambios["puente_hdmi"] not in ("programa", "no") \
                and cambios["puente_hdmi"] not in camaras():
            return self._error("fuente_invalida")
        if cambios.get("reem_salidas") and not capacidad_gpu()["disponible"]:
            return self._error("gpu_no_disponible")
        if "rtmp" in cambios.get("reem_salidas", []) and not (
                cambios.get("reem_rtmp_url") or antes["reem_rtmp_url"]):
            return self._error("url_rtmp_invalida")
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
                evento("modo", modo=cambios["modo"], camaras=simultaneo(nucleo.CONF.get()))
            if cambios.keys() - {"modo"}:
                evento("ajustes", campos=sorted(cambios.keys() - {"modo"}))
        if "clave_panel" in cambios:
            nucleo.SESIONES.cerrar_todas(salvo=self._testigo())
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
    """IPv4 e IPv6 en el mismo socket (<equipo>.local resuelve a ambos). Un
    hilo por petición, pero no más de CONEXIONES_MAX a la vez: quien abra
    conexiones sin parar no agota los hilos ni la memoria del equipo."""
    address_family = socket.AF_INET6
    daemon_threads = True
    tls = False

    def __init__(self, *args, **kwargs):
        self.hueco = threading.BoundedSemaphore(CONEXIONES_MAX)
        super().__init__(*args, **kwargs)

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

    def process_request(self, request, client_address):
        if not self.hueco.acquire(timeout=2):
            self.shutdown_request(request)      # lleno: que reintente
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.hueco.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.hueco.release()


class ServidorTLS(Servidor):
    """El panel por HTTPS. El saludo TLS se hace en el hilo de cada petición, no
    al aceptarla: un cliente lento o que habla HTTP no frena a los demás."""
    tls = True

    def __init__(self, direccion, manejador, contexto):
        self.contexto = contexto
        super().__init__(direccion, manejador)

    def finish_request(self, request, client_address):
        request.settimeout(20)
        try:
            cifrado = self.contexto.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        try:
            self.RequestHandlerClass(cifrado, client_address, self)
        finally:
            try:
                cifrado.close()
            except OSError:
                pass
