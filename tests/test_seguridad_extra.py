# NDIICAM — pruebas de los arreglos de seguridad y robustez del servicio.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
#
#   python3 -m unittest discover -s tests -v
#
# Sin hardware: el servidor web de verdad en un puerto libre, con la
# configuración en un directorio temporal, y las funciones puras.
import base64
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(AQUI), "ndiicam"))

if "gi" not in sys.modules:         # como en test_ndiicam.py: GStreamer de mentira
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **k: None
    repo = types.ModuleType("gi.repository")
    repo.GLib = repo.Gst = types.SimpleNamespace()
    gi.repository = repo
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repo

import nucleo  # noqa: E402
import actualizador  # noqa: E402
import cortafuegos  # noqa: E402
import estado  # noqa: E402
import panel  # noqa: E402
import seguridad  # noqa: E402
import validar  # noqa: E402


class ConfigTemporal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = (nucleo.CONFIG, nucleo.CONF, nucleo.SESIONES, nucleo.INTENTOS, nucleo.HTTPS,
                         nucleo.CORTAFUEGOS, nucleo.RED)
        nucleo.CONFIG = os.path.join(self.tmp.name, "config.json")
        nucleo.CONF = nucleo.Config()
        nucleo.SESIONES = seguridad.Sesiones(os.path.join(self.tmp.name, "s.json"))
        nucleo.INTENTOS = seguridad.LimiteIntentos()
        nucleo.HTTPS = None
        nucleo.CORTAFUEGOS = cortafuegos.Cortafuegos()
        nucleo.RED = None

    def tearDown(self):
        (nucleo.CONFIG, nucleo.CONF, nucleo.SESIONES, nucleo.INTENTOS, nucleo.HTTPS,
         nucleo.CORTAFUEGOS, nucleo.RED) = self.original
        self.tmp.cleanup()


class ClaveDelAP(ConfigTemporal):
    def test_se_genera_por_equipo(self):
        c = nucleo.CONF.get()
        self.assertNotEqual(c["ap_clave"], "")
        self.assertNotEqual(c["ap_clave"], nucleo.AP_CLAVE_ANTIGUA)
        self.assertGreaterEqual(len(c["ap_clave"]), 8)
        self.assertTrue(c["ap_clave"].isprintable())
        # se guarda: otra instancia lee la misma
        self.assertEqual(nucleo.Config().get()["ap_clave"], c["ap_clave"])

    def test_la_antigua_por_defecto_se_sustituye(self):
        with open(nucleo.CONFIG, "w") as f:
            json.dump({"ap_clave": nucleo.AP_CLAVE_ANTIGUA}, f)
        self.assertNotEqual(nucleo.Config().get()["ap_clave"], nucleo.AP_CLAVE_ANTIGUA)
        with open(nucleo.CONFIG, "w") as f:
            json.dump({"ap_clave": "mi-clave-propia"}, f)
        self.assertEqual(nucleo.Config().get()["ap_clave"], "mi-clave-propia")

    def test_ni_a_mano(self):
        cambios, errores = validar.validar_config({"ap_clave": nucleo.AP_CLAVE_ANTIGUA})
        self.assertEqual(errores, {"ap_clave": "clave_wifi_invalida"})
        cambios, errores = validar.validar_config({"ap_clave": "otra-clave-larga"})
        self.assertEqual(cambios, {"ap_clave": "otra-clave-larga"})


class Validadores(unittest.TestCase):
    def test_config_campo_a_campo(self):
        cambios, errores = validar.validar_config({
            "modo": "puente", "idioma": "xx", "decodificadores": "3", "puertos_extra": "tcp:8080",
            "reem_kbps": "abc", "cortafuegos": 0, "pais": "es", "basura": 1})
        self.assertEqual(cambios["modo"], "puente")
        self.assertEqual(cambios["decodificadores"], 3)
        self.assertEqual(cambios["puertos_extra"], "tcp:8080")
        self.assertIs(cambios["cortafuegos"], False)
        self.assertEqual(cambios["pais"], "ES")
        self.assertNotIn("basura", cambios)
        self.assertEqual(errores, {"idioma": "idioma_invalido", "reem_kbps": "kbps_invalido"})

    def test_codigos_desdoblados(self):
        _, e = validar.validar_fuente({"latencia": "x", "formato": "x", "grabar_cuando": "x", "srt_latencia": 5})
        self.assertEqual(e, {"latencia": "latencia_invalida", "formato": "formato_invalido",
                             "grabar_cuando": "grabar_cuando_invalido", "srt_latencia": "srt_latencia_invalida"})
        _, e = validar.validar_config({"hdmi_consola": "x", "idioma": "x", "decodificadores": "9"})
        self.assertEqual(e, {"hdmi_consola": "consola_invalida", "idioma": "idioma_invalido",
                             "decodificadores": "decodificadores_invalido"})

    def test_url_rtmp_oculta_conserva_la_guardada(self):
        previo = {"rtmp_url": "rtmp://a.rtmp.youtube.com/live2/clave-real"}
        cambios, errores = validar.validar_fuente({"rtmp_url": "rtmp://a.rtmp.youtube.com/live2/••••"}, previo)
        self.assertEqual((cambios, errores), ({}, {}))
        cambios, _ = validar.validar_fuente({"rtmp_url": "rtmp://a.rtmp.youtube.com/live2/••••"}, {"rtmp_url": ""})
        self.assertEqual(cambios, {"rtmp_url": ""})
        _, errores = validar.validar_fuente({"rtmp_url": "http://no-vale"})
        self.assertEqual(errores, {"rtmp_url": "url_rtmp_invalida"})

    def test_copia_descarta_lo_roto_y_conserva_lo_demas(self):
        cambios, ignorados = validar.validar_copia({
            "idioma": "en", "modo": "x", "clave_panel": "texto-plano",
            "fuentes": {"cam1": {"resolucion": "720p", "gpu_kbps": "mal"}, "cam2": "no-es-dict"},
            "rutas": "no-es-lista", "entradas_red": [{"id": "a"}]})
        self.assertEqual(cambios["idioma"], "en")
        self.assertNotIn("modo", cambios)
        self.assertNotIn("clave_panel", cambios)
        self.assertEqual(cambios["fuentes"], {"cam1": {"resolucion": "720p"}})
        self.assertEqual(cambios["entradas_red"], [{"id": "a"}])
        self.assertNotIn("rutas", cambios)
        self.assertEqual(sorted(ignorados), ["clave_panel", "fuentes.cam1.gpu_kbps", "fuentes.cam2", "modo", "rutas"])
        # el resumen de una contraseña sí se restaura
        resumen = nucleo.resumen_clave("secreta")
        cambios, ignorados = validar.validar_copia({"clave_panel": resumen})
        self.assertEqual((cambios["clave_panel"], ignorados), (resumen, []))


class CuerpoDeLasPeticiones(ConfigTemporal):
    """Content-Length negativo, no numérico o mayor de lo admitido."""

    def setUp(self):
        super().setUp()
        self.servidor = panel.Servidor(("::", 0), panel.Panel)
        self.puerto = self.servidor.server_address[1]
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()

    def tearDown(self):
        self.servidor.shutdown()
        self.servidor.server_close()
        super().tearDown()

    def crudo(self, cabeceras, cuerpo=b""):
        """Una petición escrita a mano por el socket; devuelve (estado, json)."""
        so = socket.create_connection(("127.0.0.1", self.puerto), timeout=10)
        so.sendall(b"POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                   + cabeceras + b"\r\n" + cuerpo)
        datos = b""
        while b"\r\n\r\n" not in datos:
            trozo = so.recv(4096)
            if not trozo:
                break
            datos += trozo
        cab, _, resto = datos.partition(b"\r\n\r\n")
        n = int([l for l in cab.split(b"\r\n") if l.lower().startswith(b"content-length")][0].split(b":")[1])
        while len(resto) < n:
            resto += so.recv(4096)
        so.close()
        return int(cab.split()[1]), json.loads(resto)

    def test_content_length_negativo_no_lee_hasta_el_infinito(self):
        t = time.monotonic()
        estado_http, d = self.crudo(b"Content-Length: -1\r\n", b"x" * 100000)
        self.assertEqual((estado_http, d["error"]), (413, "cuerpo_grande"))
        self.assertLess(time.monotonic() - t, 5)

    def test_content_length_no_numerico_y_grande(self):
        estado_http, d = self.crudo(b"Content-Length: abc\r\n")
        self.assertEqual((estado_http, d["error"]), (413, "cuerpo_grande"))
        estado_http, d = self.crudo(b"Content-Length: 999999\r\n")
        self.assertEqual((estado_http, d["error"]), (413, "cuerpo_grande"))

    def test_json_roto(self):
        estado_http, d = self.crudo(b"Content-Length: 5\r\n", b"{{{{{")
        self.assertEqual((estado_http, d["error"]), (400, "json_invalido"))

    def test_sin_cuerpo_sigue_valiendo(self):
        estado_http, d = self.crudo(b"Content-Length: 0\r\n")
        self.assertEqual(estado_http, 403)              # no hay contraseña aún: clave_incorrecta
        self.assertEqual(d["error"], "clave_incorrecta")


class AccesoDesdeElAP(ConfigTemporal):
    """La WiFi de configuración ya no da el panel sin contraseña, salvo en la
    ventana de recuperación."""

    class Falso(panel.Panel):
        """Un manejador sin socket: solo lo que mira _recuperacion_ap."""
        def __init__(self, ip):
            self.client_address = (ip, 0)
            self.headers = {}
            self.server = types.SimpleNamespace(tls=False)

    def test_sin_contrasena_entra(self):
        h = self.Falso("10.42.0.20")
        self.assertTrue(h._recuperacion_ap())
        self.assertTrue(h._autorizado())
        self.assertFalse(self.Falso("192.168.1.5")._recuperacion_ap())

    def test_con_contrasena_solo_en_la_ventana(self):
        nucleo.CONF.actualizar({"clave_panel": nucleo.resumen_clave("una-clave-larga")})
        h = self.Falso("10.42.0.20")
        nucleo.RED = types.SimpleNamespace(ap_activo=True, ap_desde=time.time() - 30)
        self.assertTrue(h._recuperacion_ap())
        nucleo.RED = types.SimpleNamespace(ap_activo=True, ap_desde=time.time() - panel.RECUPERACION_AP - 1)
        self.assertFalse(h._recuperacion_ap())
        self.assertFalse(h._autorizado())
        nucleo.RED = types.SimpleNamespace(ap_activo=False, ap_desde=time.time())
        self.assertFalse(h._recuperacion_ap())


class RespaldoYSesion(ConfigTemporal):
    """El servidor de verdad: la copia pide la contraseña; Basic Auth se cachea."""

    def setUp(self):
        super().setUp()
        import http.client
        self.http = http.client
        self.servidor = panel.Servidor(("::", 0), panel.Panel)
        self.puerto = self.servidor.server_address[1]
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()

    def tearDown(self):
        self.servidor.shutdown()
        self.servidor.server_close()
        super().tearDown()

    def pedir(self, metodo, ruta, datos=None, cookie=None, cabeceras=None):
        c = self.http.HTTPConnection("127.0.0.1", self.puerto, timeout=10)
        h = {"Content-Type": "application/json", **(cabeceras or {})}
        if cookie:
            h["Cookie"] = cookie
        c.request(metodo, ruta, json.dumps(datos) if datos is not None else None, h)
        r = c.getresponse()
        cuerpo = r.read()
        c.close()
        return r, cuerpo

    def test_descargar_la_copia_pide_la_contrasena(self):
        r, _ = self.pedir("POST", "/api/clave/crear", {"clave": "una-clave-larga"})
        cookie = r.getheader("Set-Cookie").split(";")[0]
        r, cuerpo = self.pedir("GET", "/api/respaldo", cookie=cookie)
        self.assertEqual(r.status, 405)                 # ya no se descarga por GET
        r, cuerpo = self.pedir("POST", "/api/respaldo/descargar", {"clave_actual": "mal"}, cookie=cookie)
        self.assertEqual((r.status, json.loads(cuerpo)["error"]), (403, "clave_actual_incorrecta"))
        # con la contraseña: el JSON de la copia, con la red de mentira
        nucleo.RED = types.SimpleNamespace(wifi=None, ap_activo=False, ap_desde=0.0, guardadas=lambda **k: [])
        r, cuerpo = self.pedir("POST", "/api/respaldo/descargar", {"clave_actual": "una-clave-larga"}, cookie=cookie)
        self.assertEqual(r.status, 200)
        self.assertIn("attachment", r.getheader("Content-Disposition"))
        self.assertIn("config", json.loads(cuerpo))

    def test_basic_auth_cacheada(self):
        nucleo.CONF.actualizar({"clave_panel": nucleo.resumen_clave("una-clave-larga")})
        cab = {"Authorization": "Basic " + base64.b64encode(b"x:una-clave-larga").decode()}
        llamadas = []
        original = panel.clave_correcta
        panel.clave_correcta = lambda *a: llamadas.append(1) or original(*a)
        try:
            for _ in range(3):
                r, _ = self.pedir("GET", "/api/grabaciones", cabeceras=cab)
                self.assertEqual(r.status, 200)
        finally:
            panel.clave_correcta = original
            panel._AUTH_CACHE.clear()
        self.assertEqual(len(llamadas), 1)              # PBKDF2 una sola vez en 60 s

    def test_pagina_comprimida_con_etag_y_csp(self):
        r, cuerpo = self.pedir("GET", "/", cabeceras={"Accept-Encoding": "gzip"})
        self.assertEqual((r.status, r.getheader("Content-Encoding")), (200, "gzip"))
        etag = r.getheader("ETag")
        self.assertTrue(etag)
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("script-src 'sha256-", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        r, cuerpo = self.pedir("GET", "/", cabeceras={"If-None-Match": etag})
        self.assertEqual(r.status, 304)
        r, cuerpo = self.pedir("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIsNone(r.getheader("Content-Encoding"))
        self.assertIn(b"<!doctype", cuerpo.lower())

    def test_sin_espacio_devuelve_507(self):
        r, _ = self.pedir("POST", "/api/clave/crear", {"clave": "una-clave-larga"})
        cookie = r.getheader("Set-Cookie").split(";")[0]
        original = nucleo.Config._guardar
        nucleo.Config._guardar = lambda self: (_ for _ in ()).throw(nucleo.SinEspacio("lleno"))
        try:
            r, cuerpo = self.pedir("POST", "/api/config", {"idioma": "en"}, cookie=cookie)
        finally:
            nucleo.Config._guardar = original
        self.assertEqual((r.status, json.loads(cuerpo)["error"]), (507, "sin_espacio"))
        self.assertEqual(nucleo.CONF.get()["idioma"], "en")      # en memoria sí se aplicó


class CortafuegosReenvio(unittest.TestCase):
    def test_la_wifi_de_configuracion_no_es_un_puente(self):
        g = cortafuegos.reglas("emisor", [], [], [22])
        self.assertIn("type filter hook forward priority filter; policy accept;", g)
        self.assertIn('iifname "ap0" drop', g)
        self.assertIn('oifname "ap0" drop', g)
        # y la cadena de entrada sigue como estaba
        self.assertIn("type filter hook input priority filter; policy drop;", g)


class ClaveRtmpEnRutas(ConfigTemporal):
    def test_estado_rutas_la_tapa(self):
        nucleo.CONF.actualizar({"motor": "modos", "rutas": [
            {"id": "r1", "nombre": "Culto", "video": "", "audio": "", "salidas": [
                {"id": "s1", "tipo": "rtmp", "url": "rtmp://a.rtmp.youtube.com/live2/clave-secreta"}]}]})
        nucleo.FUENTES = types.SimpleNamespace(motor=types.SimpleNamespace(
            entradas_actuales=lambda cams, cfg: [], limpias=None, errores={}, estado=lambda: {},
            ultimo={"activas": [], "pausadas": []}))
        nucleo.BUSCADOR = None
        try:
            d = estado.estado_rutas()
        finally:
            nucleo.FUENTES = None
        url = d["rutas"][0]["salidas"][0]["url"]
        self.assertNotIn("clave-secreta", url)
        self.assertIn("•", url)
        self.assertEqual((d["limite_rutas"], d["limite_entradas"]), (16, 32))


class Actualizador(unittest.TestCase):
    def test_solo_versiones_como_tag(self):
        for bueno in ("v0.6.0", "1.2.3"):
            self.assertTrue(actualizador.TAG_VERSION.match(bueno), bueno)
        for malo in ("v0.6", "main", "v0.6.0;rm", "../x", "v0.6.0 ", ""):
            self.assertFalse(actualizador.TAG_VERSION.match(malo), malo)


class Certificado(unittest.TestCase):
    def test_vigencia(self):
        ahora = 1_800_000_000
        original = seguridad.vigencia_certificado
        try:
            seguridad.vigencia_certificado = lambda ruta=None: (ahora - 10, ahora + 365 * 86400)
            self.assertTrue(seguridad.certificado_vigente(ahora))
            seguridad.vigencia_certificado = lambda ruta=None: (ahora - 10, ahora + 10 * 86400)
            self.assertFalse(seguridad.certificado_vigente(ahora))        # caduca en 10 días
            seguridad.vigencia_certificado = lambda ruta=None: (ahora + 3600, ahora + 365 * 86400)
            self.assertFalse(seguridad.certificado_vigente(ahora))        # aún no vale (reloj)
            seguridad.vigencia_certificado = lambda ruta=None: None
            self.assertFalse(seguridad.certificado_vigente(ahora))
        finally:
            seguridad.vigencia_certificado = original


class Latidos(unittest.TestCase):
    def test_bucles_vivos(self):
        nucleo.LATIDOS.clear()
        self.assertTrue(nucleo.bucles_vivos(10))
        nucleo.latido("a")
        self.assertTrue(nucleo.bucles_vivos(10))
        nucleo.LATIDOS["b"] = time.monotonic() - 100
        self.assertFalse(nucleo.bucles_vivos(10))
        nucleo.latido("b", fin=True)
        self.assertTrue(nucleo.bucles_vivos(10))
        nucleo.LATIDOS.clear()


class EscrituraPrivada(unittest.TestCase):
    def test_eventos_privados(self):
        tmp = tempfile.TemporaryDirectory()
        original = nucleo.EVENTOS_ARCHIVO
        nucleo.EVENTOS_ARCHIVO = os.path.join(tmp.name, "eventos.jsonl")
        try:
            nucleo.evento("prueba", x=1)
            self.assertEqual(os.stat(nucleo.EVENTOS_ARCHIVO).st_mode & 0o777, 0o600)
        finally:
            nucleo.EVENTOS_ARCHIVO = original
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
