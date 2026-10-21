# NDIICAM — pruebas de la lógica que no necesita hardware ni GStreamer.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
#
#   python3 -m unittest discover -s tests -v
#
# Corren en cualquier máquina con Python 3 y PyYAML. Lo que toca cámaras,
# GStreamer, NetworkManager o la GPU no se prueba aquí: eso se prueba en un
# equipo real.
import json
import os
import sys
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(AQUI), "ndiicam"))

# fuente.py importa GStreamer al cargarse; para probar sus funciones puras basta un gi de mentira
if "gi" not in sys.modules:
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **k: None
    repo = types.ModuleType("gi.repository")
    repo.GLib = repo.Gst = types.SimpleNamespace()
    gi.repository = repo
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repo

import nucleo  # noqa: E402
import camaras  # noqa: E402
import estado  # noqa: E402
import fuente  # noqa: E402
import puente  # noqa: E402
import supervisor  # noqa: E402


class Utilidades(unittest.TestCase):
    def test_version_tupla(self):
        self.assertEqual(nucleo.version_tupla("v0.3.0"), (0, 3, 0))
        self.assertEqual(nucleo.version_tupla("0.10.2"), (0, 10, 2))
        self.assertLess(nucleo.version_tupla("0.3.0"), nucleo.version_tupla("0.10.0"))
        self.assertEqual(nucleo.version_tupla(None), ())

    def test_nmcli(self):
        self.assertEqual(nucleo.partir("wlp2s0:wifi:connected:ofc\\:AC"), ["wlp2s0", "wifi", "connected", "ofc:AC"])
        self.assertEqual(nucleo.partir(""), [""])
        self.assertEqual(nucleo.nm_valores("192.168.1.90/24 | 10.42.0.1/24\n"), ["192.168.1.90/24", "10.42.0.1/24"])
        self.assertEqual(nucleo.nm_valores(" "), [])

    def test_clave_del_panel(self):
        guardada = nucleo.resumen_clave("secreta")
        self.assertNotIn("secreta", guardada)
        self.assertTrue(nucleo.clave_correcta("secreta", guardada))
        self.assertFalse(nucleo.clave_correcta("otra", guardada))
        self.assertFalse(nucleo.clave_correcta("secreta", "basura-sin-formato"))

    def test_ocultar_clave_rtmp(self):
        self.assertEqual(supervisor.ocultar_clave_rtmp(""), "")
        oculta = supervisor.ocultar_clave_rtmp("rtmp://a.rtmp.youtube.com/live2/abcd-1234")
        self.assertTrue(oculta.startswith("rtmp://a.rtmp.youtube.com/live2/"))
        self.assertNotIn("abcd-1234", oculta)

    def test_ruta_preview_estable(self):
        a = supervisor.ruta_preview("usb-DJI_DJIPocket3_123-video-index0")
        self.assertEqual(a, supervisor.ruta_preview("usb-DJI_DJIPocket3_123-video-index0"))
        self.assertNotEqual(a, supervisor.ruta_preview("otra"))
        self.assertTrue(a.startswith("/run/ndiicam/preview-") and a.endswith(".jpg"))

    def test_validadores(self):
        self.assertTrue(nucleo.NOMBRE_NDI.match("Cámara 1 - lateral"))
        self.assertFalse(nucleo.NOMBRE_NDI.match("x" * 49))
        self.assertFalse(nucleo.NOMBRE_NDI.match("con/barra"))
        self.assertTrue(nucleo.URL_RTMP.match("rtmp://a.rtmp.youtube.com/live2/clave"))
        self.assertTrue(nucleo.URL_RTMP.match("rtmps://live.twitch.tv/app/clave"))
        self.assertFalse(nucleo.URL_RTMP.match("http://no-es-rtmp"))
        self.assertTrue(nucleo.CLAVE_PUENTE.match("Abc123xyZ_9"))
        self.assertFalse(nucleo.CLAVE_PUENTE.match("con espacio"))
        self.assertTrue(nucleo.NOMBRE_GRABACION.match("NDIICAM_2026-10-20_23-38-17.mp4"))
        self.assertFalse(nucleo.NOMBRE_GRABACION.match("../etc/passwd.mp4"))
        self.assertFalse(nucleo.NOMBRE_GRABACION.match("nota.txt"))
        self.assertTrue(nucleo.NOMBRE_EQUIPO.match("camara-1"))
        self.assertFalse(nucleo.NOMBRE_EQUIPO.match("-malo"))


class Configuracion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = nucleo.CONFIG
        nucleo.CONFIG = os.path.join(self.tmp.name, "config.json")

    def tearDown(self):
        nucleo.CONFIG = self.original
        self.tmp.cleanup()

    def test_por_defecto_y_por_camara(self):
        c = nucleo.Config()
        f = c.fuente("cam1")
        self.assertEqual(f["resolucion"], "1080p")
        self.assertEqual(f["gpu_salida"], "no")
        self.assertTrue(f["activa"])
        c.actualizar_fuente("cam1", {"resolucion": "720p", "gpu_salida": "grabar", "basura": 1})
        f = c.fuente("cam1")
        self.assertEqual(f["resolucion"], "720p")
        self.assertEqual(f["gpu_salida"], "grabar")
        self.assertNotIn("basura", f)                 # solo las claves conocidas
        self.assertEqual(c.fuente("cam2")["resolucion"], "1080p")
        # lo guardado se vuelve a leer
        with open(nucleo.CONFIG) as fh:
            self.assertEqual(json.load(fh)["fuentes"]["cam1"]["resolucion"], "720p")
        self.assertEqual(nucleo.Config().fuente("cam1")["resolucion"], "720p")

    def test_fichero_privado(self):
        nucleo.Config().actualizar({"idioma": "en"})
        self.assertEqual(os.stat(nucleo.CONFIG).st_mode & 0o777, 0o600)

    def test_restablecer(self):
        c = nucleo.Config()
        c.actualizar({"modo": "puente"})
        c.restablecer()
        self.assertEqual(c.get()["modo"], "emisor")
        self.assertFalse(os.path.exists(nucleo.CONFIG))


class ModoDeCamara(unittest.TestCase):
    """Fuentes.objetivo elige resolución, fps y formato entre los modos de la cámara."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = nucleo.CONFIG
        nucleo.CONFIG = os.path.join(self.tmp.name, "config.json")
        self.conf = nucleo.Config()
        self.sup = supervisor.Fuentes(self.conf)
        self.sup.audio = []

    def tearDown(self):
        nucleo.CONFIG = self.original
        self.tmp.cleanup()

    def fuente(self, modos):
        return supervisor.Fuente({"id": "cam", "nombre": "Cam", "dev": "/dev/video9", "usb": "1-2",
                                  "modos": modos})

    def test_prefiere_mjpeg_a_la_resolucion_pedida(self):
        f = self.fuente([(1920, 1080, 30, "MJPG"), (1920, 1080, 5, "YUYV"), (1280, 720, 60, "MJPG")])
        o = self.sup.objetivo(f)
        self.assertEqual((o["ancho"], o["alto"], o["fps"], o["fmt"]), (1920, 1080, 30, "MJPG"))
        self.assertIsNotNone(o["preview"])

    def test_sin_1080p_coge_el_mayor_apaisado(self):
        f = self.fuente([(1280, 720, 30, "MJPG"), (640, 480, 30, "YUYV")])
        o = self.sup.objetivo(f)
        self.assertEqual((o["ancho"], o["alto"]), (1280, 720))

    def test_fps_no_supera_lo_pedido(self):
        self.conf.actualizar_fuente("cam", {"fps": 25})
        f = self.fuente([(1920, 1080, 60, "MJPG"), (1920, 1080, 24, "MJPG")])
        self.assertEqual(self.sup.objetivo(f)["fps"], 24)

    def test_solo_yuyv_sin_gpu_con_preview(self):
        f = self.fuente([(1280, 720, 30, "YUYV")])
        o = self.sup.objetivo(f)
        self.assertEqual(o["fmt"], "YUYV")
        self.assertIsNotNone(o["preview"])          # sale de un fotograma reducido
        self.assertIsNone(o["gpu"])

    def test_formato_segun_el_puerto(self):
        modos = [(1920, 1080, 30, "MJPG"), (1920, 1080, 30, "YUYV")]
        original = supervisor.velocidad_usb
        try:
            supervisor.velocidad_usb = lambda ruta: 480           # USB 2.0
            self.assertEqual(self.sup.objetivo(self.fuente(modos))["fmt"], "MJPG")
            supervisor.velocidad_usb = lambda ruta: 5000          # USB 3.0
            self.assertEqual(self.sup.objetivo(self.fuente(modos))["fmt"], "YUYV")
            # la salida por GPU parte del JPEG: en automático se queda en MJPEG
            self.conf.actualizar_fuente("cam", {"gpu_salida": "grabar"})
            self.assertEqual(self.sup.objetivo(self.fuente(modos))["fmt"], "MJPG")
            # forzado a mano, gana el ajuste
            self.conf.actualizar_fuente("cam", {"gpu_salida": "no", "formato": "mjpeg"})
            self.assertEqual(self.sup.objetivo(self.fuente(modos))["fmt"], "MJPG")
            supervisor.velocidad_usb = lambda ruta: 480
            self.conf.actualizar_fuente("cam", {"formato": "crudo"})
            self.assertEqual(self.sup.objetivo(self.fuente(modos))["fmt"], "YUYV")
        finally:
            supervisor.velocidad_usb = original

    def test_horario_de_grabacion(self):
        self.assertTrue(supervisor.en_horario("10:00", "09:00", "18:00"))
        self.assertFalse(supervisor.en_horario("18:00", "09:00", "18:00"))     # el final no entra
        self.assertFalse(supervisor.en_horario("08:59", "09:00", "18:00"))
        self.assertTrue(supervisor.en_horario("23:30", "22:00", "06:00"))      # pasa de medianoche
        self.assertTrue(supervisor.en_horario("05:59", "22:00", "06:00"))
        self.assertFalse(supervisor.en_horario("12:00", "22:00", "06:00"))
        self.assertTrue(supervisor.en_horario("12:00", "08:00", "08:00"))      # igual: todo el día

    def test_regla_de_grabacion_en_la_salida_gpu(self):
        self.conf.actualizar_fuente("cam", {"gpu_salida": "grabar", "grabar_cuando": "horario",
                                            "grabar_desde": "22:00", "grabar_hasta": "06:00"})
        aj = self.conf.fuente("cam")
        self.assertEqual(aj["grabar_cuando"], "horario")
        self.assertTrue(nucleo.HORA.match("22:00"))
        self.assertFalse(nucleo.HORA.match("24:00"))
        self.assertFalse(nucleo.HORA.match("9:00"))

    def test_salida_gpu_sin_url_rtmp_no_arranca(self):
        aj = {"gpu_salida": "rtmp", "gpu_kbps": 8000, "srt_puerto": 9000, "rtmp_url": ""}
        self.assertIsNone(supervisor.Fuentes.salida_gpu(aj, "MJPG"))


class Puente(unittest.TestCase):
    def test_xml_para_atem(self):
        xml = puente.xml_atem("192.168.1.50", "clave123")
        raiz = ET.fromstring(xml)
        self.assertEqual(raiz.tag, "streaming")
        self.assertEqual(raiz.find("service/servers/server/url").text, "rtmp://192.168.1.50:1935/live")
        self.assertEqual(raiz.find("service/key").text, "clave123")
        configs = raiz.findall("service/profiles/profile/config")
        self.assertEqual({c.get("fps") for c in configs}, {"60", "50", "30", "25", "24"})
        self.assertTrue(all(c.get("resolution") == "1080p" for c in configs))


class JpegDeLaCamara(unittest.TestCase):
    """muestreo_jpeg lee el submuestreo de la cabecera SOF0: la GPU lo exige."""

    @staticmethod
    def jpeg(marca_sof=0xC0, componentes=3, hv=0x22):
        datos = b"\xff\xd8" + b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00" + b"\x00" * 9
        largo = 8 + 3 * componentes
        sof = bytes([0xFF, marca_sof]) + largo.to_bytes(2, "big") + b"\x08" + (1080).to_bytes(2, "big") \
            + (1920).to_bytes(2, "big") + bytes([componentes])
        for i in range(componentes):
            sof += bytes([i + 1, hv if i == 0 else 0x11, 0])
        return datos + sof + b"\xff\xda" + b"\x00\x0c" + b"\x00" * 10

    def test_formatos(self):
        self.assertEqual(fuente.muestreo_jpeg(self.jpeg()), "YCbCr-4:2:0")
        self.assertEqual(fuente.muestreo_jpeg(self.jpeg(hv=0x21)), "YCbCr-4:2:2")
        self.assertEqual(fuente.muestreo_jpeg(self.jpeg(hv=0x11)), "YCbCr-4:4:4")
        self.assertEqual(fuente.muestreo_jpeg(self.jpeg(componentes=1)), "GRAYSCALE")

    def test_progresivo_o_roto(self):
        self.assertIsNone(fuente.muestreo_jpeg(self.jpeg(marca_sof=0xC2)))   # progresivo: la GPU no
        self.assertIsNone(fuente.muestreo_jpeg(b"no es un jpeg"))
        self.assertIsNone(fuente.muestreo_jpeg(b""))


class NivelDeAudio(unittest.TestCase):
    """nivel_audio lee los mensajes de 'level' aunque GValueArray no pase a Python."""

    class Estructura:
        def __init__(self, nombre, texto):
            self.nombre, self.texto = nombre, texto

        def get_name(self):
            return self.nombre

        def to_string(self):
            return self.texto

        def get_value(self, campo):
            raise TypeError("GValueArray")

    def mensaje(self, nombre, texto):
        return types.SimpleNamespace(get_structure=lambda: self.Estructura(nombre, texto))

    def test_lee_del_texto(self):
        texto = ("level, endtime=(guint64)200000000, timestamp=(guint64)0, stream-time=(guint64)0,"
                 " running-time=(guint64)0, duration=(guint64)200000000,"
                 " rms=(GValueArray)< -23.5, -24.1 >, peak=(GValueArray)< -12.25, -inf >,"
                 " decay=(GValueArray)< -12.25, -13 >;")
        n = fuente.nivel_audio(self.mensaje("level", texto))
        self.assertEqual(n, {"peak": -12.2, "rms": -23.5})        # el mayor canal, redondeado

    def test_otros_mensajes_no(self):
        self.assertIsNone(fuente.nivel_audio(self.mensaje("otra-cosa", "")))
        self.assertIsNone(fuente.nivel_audio(types.SimpleNamespace(get_structure=lambda: None)))

    def test_acota_el_rango(self):
        n = fuente.nivel_audio(self.mensaje("level", "level, rms=(GValueArray)< 3.0 >, peak=(GValueArray)< -200 >;"))
        self.assertEqual(n, {"peak": -120.0, "rms": 0.0})


class Pantallas(unittest.TestCase):
    def test_resolucion_de_la_pantalla(self):
        self.assertEqual(camaras.resolucion_pantalla({"modo": "1920x1080"}), [1920, 1080])
        self.assertIsNone(camaras.resolucion_pantalla({"modo": None}))


class Grabaciones(unittest.TestCase):
    def test_lista_ordenada_y_filtrada(self):
        original = estado.GRABACIONES
        with tempfile.TemporaryDirectory() as d:
            estado.GRABACIONES = d
            for n, t in (("a_2026-01-01_10-00-00.mp4", 100), ("b_2026-01-01_11-00-00.mp4", 200)):
                with open(os.path.join(d, n), "wb") as fh:
                    fh.write(b"x" * 10)
                os.utime(os.path.join(d, n), (t, t))
            open(os.path.join(d, "notas.txt"), "w").close()
            g = estado.grabaciones()
            self.assertEqual([x["nombre"] for x in g["lista"]], ["b_2026-01-01_11-00-00.mp4", "a_2026-01-01_10-00-00.mp4"])
            self.assertIsNotNone(g["libre"])
        estado.GRABACIONES = original
        self.assertEqual(estado.grabaciones.__name__, "grabaciones")


if __name__ == "__main__":
    unittest.main()
