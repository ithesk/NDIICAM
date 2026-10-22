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
import cortafuegos  # noqa: E402
import seguridad  # noqa: E402
import sistema  # noqa: E402
import panel  # noqa: E402
import gpu  # noqa: E402


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

    def test_modos_de_latencia(self):
        modos = [(1920, 1080, 30, "MJPG"), (1920, 1080, 30, "YUYV"), (1920, 1080, 60, "YUYV")]
        original = supervisor.velocidad_usb
        try:
            supervisor.velocidad_usb = lambda ruta: 5000
            o = self.sup.objetivo(self.fuente(modos))
            self.assertEqual((o["fps"], o["baja_latencia"]), (30, False))     # normal: lo pedido
            self.conf.actualizar_fuente("cam", {"latencia": "baja"})
            o = self.sup.objetivo(self.fuente(modos))
            self.assertEqual((o["fps"], o["fmt"]), (60, "YUYV"))              # sube a 60 sin comprimir
            self.conf.actualizar_fuente("cam", {"latencia": "minima"})
            o = self.sup.objetivo(self.fuente(modos))
            self.assertIsNone(o["audio"])
            self.assertFalse(o["baja_latencia"])                             # sin audio no hay combinador
            supervisor.velocidad_usb = lambda ruta: 480                       # USB 2.0: no sube
            self.assertEqual(self.sup.objetivo(self.fuente(modos))["fps"], 30)
        finally:
            supervisor.velocidad_usb = original

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


class Sesiones(unittest.TestCase):
    def test_crear_validar_cerrar(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = os.path.join(d, "sesiones.json")
            s = seguridad.Sesiones(ruta)
            a, b = s.crear("1.1.1.1"), s.crear("2.2.2.2")
            self.assertTrue(s.valida(a))
            self.assertFalse(s.valida("inventado"))
            self.assertFalse(s.valida(None))
            with open(ruta) as fh:
                self.assertNotIn(a, fh.read())          # en disco solo el resumen
            self.assertTrue(seguridad.Sesiones(ruta).valida(b))    # sobrevive al reinicio
            s.cerrar_todas(salvo=a)
            self.assertTrue(s.valida(a))
            self.assertFalse(s.valida(b))
            s.cerrar(a)
            self.assertFalse(s.valida(a))

    def test_caducan(self):
        with tempfile.TemporaryDirectory() as d:
            s = seguridad.Sesiones(os.path.join(d, "s.json"))
            a = s.crear("1.1.1.1")
            for v in s.datos.values():
                v["usada"] -= seguridad.DURACION_SESION + 1
            self.assertFalse(s.valida(a))


class LimiteDeIntentos(unittest.TestCase):
    def test_bloqueo_creciente_y_acierto(self):
        reloj = [1000.0]
        lim = seguridad.LimiteIntentos(reloj=lambda: reloj[0])
        for _ in range(4):
            lim.fallo("1.1.1.1")
        self.assertEqual(lim.espera("1.1.1.1"), 0)      # 4 fallos aún sin castigo
        lim.fallo("1.1.1.1")
        self.assertEqual(lim.espera("1.1.1.1"), 30)
        self.assertEqual(lim.espera("2.2.2.2"), 0)      # por IP
        reloj[0] += 31
        self.assertEqual(lim.espera("1.1.1.1"), 0)
        lim.fallo("1.1.1.1")
        self.assertEqual(lim.espera("1.1.1.1"), 60)     # cada tanda, el doble
        for _ in range(10):
            lim.fallo("1.1.1.1")
        self.assertLessEqual(lim.espera("1.1.1.1"), seguridad.LimiteIntentos.BLOQUEO_MAX)
        lim.acierto("1.1.1.1")
        reloj[0] += 61
        self.assertEqual(lim.espera("1.1.1.1"), 0)

    def test_tope_global(self):
        lim = seguridad.LimiteIntentos(reloj=lambda: 50.0)
        for i in range(seguridad.LimiteIntentos.TOPE_GLOBAL):
            lim.fallo(f"10.0.0.{i}")
        self.assertGreater(lim.espera("10.9.9.9"), 0)   # IP nueva, pero hay un ataque repartido


class Cortafuegos(unittest.TestCase):
    def test_puertos_extra(self):
        self.assertEqual(cortafuegos.puertos_extra(""), [])
        self.assertEqual(cortafuegos.puertos_extra("tcp:8080, udp:5000-5010"),
                         [("tcp", 8080, 8080), ("udp", 5000, 5010)])
        for malo in ("8080", "tcp:0", "udp:10-5", "icmp:1", "tcp:70000", "tcp:80;drop"):
            self.assertIsNone(cortafuegos.puertos_extra(malo), malo)

    def test_reglas_por_modo(self):
        emisor = cortafuegos.reglas("emisor", [9000], [], [22])
        self.assertIn("policy drop", emisor)
        self.assertIn("ct state established,related accept", emisor)
        self.assertIn("tcp dport 5959-5999 accept", emisor)
        self.assertRegex(emisor, r"udp dport \{[^}]*\b9000\b")
        self.assertNotRegex(emisor, r"\b1935\b")
        self.assertRegex(emisor, r"tcp dport \{ 22, 53, 80, 443 \}")
        self.assertNotIn("6960", emisor)
        # el receptor necesita además sus propios puertos: la fuente le abre ahí el vídeo
        receptor = cortafuegos.reglas("receptor", [9000], [], [22])
        self.assertIn("tcp dport 5959-5999 accept", receptor)
        self.assertIn("tcp dport 6960-7059 accept", receptor)
        self.assertIn("udp dport 6960-7059 accept", receptor)
        self.assertIn("udp dport 7960-8059 accept", receptor)
        self.assertNotRegex(receptor, r"\b9000\b")
        puente_ = cortafuegos.reglas("puente", [9000], [("tcp", 8080, 8080), ("udp", 7000, 7010)], [22, 2222])
        self.assertRegex(puente_, r"tcp dport \{[^}]*\b1935\b")
        self.assertRegex(puente_, r"udp dport \{[^}]*\b8890\b")
        self.assertNotIn("5959", puente_)
        self.assertNotRegex(puente_, r"\b9000\b")       # el SRT de las cámaras solo en emisor
        self.assertRegex(puente_, r"\b2222\b")
        self.assertRegex(puente_, r"\b8080\b")
        self.assertIn("udp dport 7000-7010 accept", puente_)
        # rehace la tabla de una vez: la crea si falta y la borra antes de definirla
        self.assertTrue(puente_.startswith("table inet ndiicam\ndelete table inet ndiicam\n"))

    def test_estado_lista_lo_abierto(self):
        c = cortafuegos.Cortafuegos()
        c.guion = cortafuegos.reglas("emisor", [], [], [22])
        e = c.estado({"cortafuegos": True})
        self.assertTrue(e["activo"])
        self.assertIn("tcp/443", e["abiertos"])
        self.assertIn("udp/5959-5999", e["abiertos"])


class MotivoDelArranque(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.originales = (sistema.ARRANQUE, sistema.PSTORE, sistema.id_arranque, sistema._diario_arranque,
                           sistema.leer, sistema.evento)
        sistema.ARRANQUE = os.path.join(self.tmp.name, "arranque.json")
        sistema.PSTORE = os.path.join(self.tmp.name, "pstore")
        self.diario = ([], None)
        self.bootstatus = "0"
        sistema._diario_arranque = lambda boot: self.diario
        sistema.leer = lambda ruta, defecto="": self.bootstatus if ruta.endswith("bootstatus") \
            else self.originales[4](ruta, defecto)
        sistema.evento = lambda *a, **k: None

    def tearDown(self):
        (sistema.ARRANQUE, sistema.PSTORE, sistema.id_arranque, sistema._diario_arranque,
         sistema.leer, sistema.evento) = self.originales
        self.tmp.cleanup()

    def arrancar(self, boot):
        sistema.id_arranque = lambda: boot
        return sistema.calcular_motivo()["codigo"]

    def test_secuencia(self):
        a, b, c, d = ("a" * 32, "b" * 32, "c" * 32, "d" * 32)
        self.assertEqual(self.arrancar(a), "primero")
        self.assertEqual(self.arrancar(a), "primero")           # reiniciar el servicio no cambia nada
        sistema.marcar_fin("reinicio_panel")
        sistema.marcar_fin("reinicio", sobrescribir=False)    # el apagado de systemd no lo pisa
        self.assertEqual(self.arrancar(b), "reinicio_panel")
        self.diario = (["systemd: Stopping...", "systemd: Reached target shutdown.target - System Shutdown."], 100.0)
        self.assertEqual(self.arrancar(c), "reinicio")          # apagado por SSH: se ve en el diario
        # la sesión SSH de un usuario también "llega a shutdown.target": eso no es apagar
        self.diario = (["systemd: Reached target shutdown.target - Shutdown.", "sshd: algo"], 100.0)
        self.assertEqual(self.arrancar(d), "inesperado")        # corte de luz
        self.bootstatus = str(sistema.WDIOF_CARDRESET)
        self.assertEqual(self.arrancar("e" * 32), "watchdog")

    def test_panico_por_pstore(self):
        self.arrancar("a" * 32)
        with open(sistema.ARRANQUE) as fh:
            t = json.load(fh)["t"]
        carpeta = os.path.join(sistema.PSTORE, str(int(t) + 30), "001")
        os.makedirs(carpeta)
        with open(os.path.join(carpeta, "dmesg.txt"), "w") as fh:
            fh.write("Panic#1 Part1\n<0>Kernel panic - not syncing: sysrq triggered crash\n")
        self.diario = (["algo"], t + 25)
        sistema.id_arranque = lambda: "b" * 32
        m = sistema.calcular_motivo()
        self.assertEqual(m["codigo"], "panico")
        self.assertIn("Kernel panic", m["detalle"])
        # el mismo volcado no vuelve a contar en el arranque siguiente
        self.diario = (["systemd: Reached target reboot.target - System Reboot."], t + 90)
        sistema.id_arranque = lambda: "c" * 32
        self.assertEqual(sistema.calcular_motivo()["codigo"], "reinicio")


class EventosGuardados(unittest.TestCase):
    def test_sobreviven_y_toleran_ceros(self):
        original = nucleo.EVENTOS_ARCHIVO
        with tempfile.TemporaryDirectory() as d:
            nucleo.EVENTOS_ARCHIVO = os.path.join(d, "eventos.jsonl")
            nucleo.EVENTOS.clear()
            nucleo.evento("inicio", version="x")
            nucleo.evento("modo", modo="puente")
            with open(nucleo.EVENTOS_ARCHIVO, "a") as fh:
                fh.write("\x00" * 50 + "\n")             # lo que deja un cuelgue a medias
            nucleo.EVENTOS.clear()
            nucleo.cargar_eventos()
            self.assertEqual([e["c"] for e in nucleo.EVENTOS], ["modo", "inicio"])
        nucleo.EVENTOS_ARCHIVO = original
        nucleo.EVENTOS.clear()


class AccesoAlPanel(unittest.TestCase):
    """El servidor de verdad, en local, con la configuración en un directorio temporal."""

    def setUp(self):
        import http.client
        import threading
        self.http = http.client
        self.tmp = tempfile.TemporaryDirectory()
        self.original = (nucleo.CONFIG, nucleo.CONF, nucleo.SESIONES, nucleo.INTENTOS, nucleo.HTTPS,
                         nucleo.CORTAFUEGOS)
        nucleo.CONFIG = os.path.join(self.tmp.name, "config.json")
        nucleo.CONF = nucleo.Config()
        nucleo.SESIONES = seguridad.Sesiones(os.path.join(self.tmp.name, "s.json"))
        nucleo.INTENTOS = seguridad.LimiteIntentos()
        nucleo.HTTPS = None
        nucleo.CORTAFUEGOS = cortafuegos.Cortafuegos()
        self.servidor = panel.Servidor(("::", 0), panel.Panel)
        self.puerto = self.servidor.server_address[1]
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()

    def tearDown(self):
        self.servidor.shutdown()
        self.servidor.server_close()
        (nucleo.CONFIG, nucleo.CONF, nucleo.SESIONES, nucleo.INTENTOS, nucleo.HTTPS,
         nucleo.CORTAFUEGOS) = self.original
        self.tmp.cleanup()

    def pedir(self, metodo, ruta, datos=None, cookie=None, cabeceras=None):
        c = self.http.HTTPConnection("127.0.0.1", self.puerto, timeout=10)
        h = {"Content-Type": "application/json", **(cabeceras or {})}
        if cookie:
            h["Cookie"] = cookie
        c.request(metodo, ruta, json.dumps(datos) if datos is not None else None, h)
        r = c.getresponse()
        cuerpo = r.read()
        c.close()
        return r, json.loads(cuerpo) if r.getheader("Content-Type", "").startswith("application/json") else cuerpo

    def test_primera_vez_entrada_y_bloqueo(self):
        r, d = self.pedir("GET", "/api/sesion")
        self.assertEqual((d["autenticado"], d["clave_creada"]), (False, False))
        r, _ = self.pedir("GET", "/api/grabaciones")
        self.assertEqual(r.status, 401)                    # sin contraseña no hay panel
        r, _ = self.pedir("GET", "/")
        self.assertEqual(r.status, 200)                    # la página sí: enseña la entrada
        r, d = self.pedir("POST", "/api/clave/crear", {"clave": "corta"})
        self.assertEqual(d["error"], "clave_panel_corta")
        r, d = self.pedir("POST", "/api/clave/crear", {"clave": "una-clave-larga"})
        self.assertEqual(r.status, 200)
        cookie = r.getheader("Set-Cookie").split(";")[0]
        self.assertIn("HttpOnly", r.getheader("Set-Cookie"))
        self.assertIn("SameSite=Strict", r.getheader("Set-Cookie"))
        r, _ = self.pedir("GET", "/api/grabaciones", cookie=cookie)
        self.assertEqual(r.status, 200)
        r, d = self.pedir("POST", "/api/clave/crear", {"clave": "otra-clave-larga"})
        self.assertEqual(r.status, 409)                    # no se puede volver a crear
        # contraseña mal 5 veces: bloqueo
        for _ in range(4):
            r, d = self.pedir("POST", "/api/login", {"clave": "mal"})
            self.assertEqual(r.status, 403)
        r, d = self.pedir("POST", "/api/login", {"clave": "mal"})
        self.assertEqual(r.status, 429)
        r, d = self.pedir("POST", "/api/login", {"clave": "una-clave-larga"})
        self.assertEqual((r.status, d["error"]), (429, "demasiados_intentos"))   # ni la buena, hasta que pase

    def test_basic_auth_y_origen(self):
        nucleo.CONF.actualizar({"clave_panel": nucleo.resumen_clave("una-clave-larga")})
        import base64
        bueno = {"Authorization": "Basic " + base64.b64encode(b"x:una-clave-larga").decode()}
        r, _ = self.pedir("GET", "/api/grabaciones", cabeceras=bueno)
        self.assertEqual(r.status, 200)
        r, d = self.pedir("POST", "/api/login", {"clave": "una-clave-larga"},
                          cabeceras={"Origin": "http://otra-web.example"})
        self.assertEqual((r.status, d["error"]), (403, "origen"))

    def test_cambiar_clave_pide_la_actual(self):
        r, _ = self.pedir("POST", "/api/clave/crear", {"clave": "una-clave-larga"})
        cookie = r.getheader("Set-Cookie").split(";")[0]
        otra = nucleo.SESIONES.crear("9.9.9.9")
        r, d = self.pedir("POST", "/api/config", {"clave_panel": "nueva-clave-larga", "clave_actual": "mal"},
                          cookie=cookie)
        self.assertEqual(d["error"], "clave_actual_incorrecta")
        r, d = self.pedir("POST", "/api/config", {"clave_panel": "nueva-clave-larga",
                                                  "clave_actual": "una-clave-larga"}, cookie=cookie)
        self.assertEqual(r.status, 200)
        self.assertFalse(nucleo.SESIONES.valida(otra))      # las demás sesiones, fuera
        r, _ = self.pedir("GET", "/api/grabaciones", cookie=cookie)
        self.assertEqual(r.status, 200)                     # la de quien la cambió sigue


class GpuParaDecodificar(unittest.TestCase):
    """capacidad_vpp: la GPU decodifica solo si se midió que compensa, y nunca en
    las GPU vetadas o en las que colgaron el equipo midiendo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = (gpu.VPP_ARCHIVO, gpu.elementos_va, gpu.ids_gpu, gpu.leer,
                     gpu.clave_vpp, gpu._medir_aparte, gpu.evento)
        gpu.VPP_ARCHIVO = os.path.join(self.tmp.name, "gpu-vpp.json")
        gpu.elementos_va = lambda: frozenset({"vapostproc", "vajpegdec", "vah264dec"})
        gpu.ids_gpu = lambda: {0x3ea0}
        self.arranque = "arranque-1"
        gpu.leer = lambda ruta, defecto="": self.arranque if "boot_id" in ruta else defecto
        gpu.clave_vpp = lambda: "clave"
        self.medidas = []
        gpu._medir_aparte = lambda clave: self.medidas.append(clave)
        self.eventos = []
        gpu.evento = lambda c, **d: self.eventos.append(c)
        gpu._VPP.clear()
        threading_orig = gpu.threading.Thread
        self.thread_orig = threading_orig

        class Inmediato:
            def __init__(s, target, args=(), daemon=None):
                s.t, s.a = target, args

            def start(s):
                s.t(*s.a)
        gpu.threading.Thread = Inmediato

    def tearDown(self):
        (gpu.VPP_ARCHIVO, gpu.elementos_va, gpu.ids_gpu, gpu.leer,
         gpu.clave_vpp, gpu._medir_aparte, gpu.evento) = self.orig
        gpu.threading.Thread = self.thread_orig
        gpu._VPP.clear()
        self.tmp.cleanup()

    def guardar(self, datos):
        with open(gpu.VPP_ARCHIVO, "w") as fh:
            json.dump(datos, fh)

    def test_sin_vpp_nada(self):
        gpu.elementos_va = lambda: frozenset({"vajpegdec", "vah264dec"})
        self.assertEqual(gpu.capacidad_vpp(), {"vpp": False, "jpeg": False, "h264": False})
        self.assertEqual(self.medidas, [])

    def test_gpu_vetada_ni_se_mide(self):
        gpu.ids_gpu = lambda: {0x5a85}          # Apollo Lake (J3355): colgó el equipo
        c = gpu.capacidad_vpp()
        self.assertFalse(c["jpeg"] or c["h264"])
        self.assertTrue(c["vetada"])
        self.assertEqual(self.medidas, [])

    def test_sin_medir_mide_y_mientras_va_por_cpu(self):
        c = gpu.capacidad_vpp()
        self.assertEqual(self.medidas, ["clave"])
        self.assertFalse(c["jpeg"] or c["h264"])
        self.assertTrue(c["midiendo"])
        # la marca queda en disco antes de medir, con el arranque
        with open(gpu.VPP_ARCHIVO) as fh:
            marca = json.load(fh)
        self.assertTrue(marca["midiendo"])
        self.assertEqual(marca["arranque"], "arranque-1")

    def test_usa_lo_medido(self):
        self.guardar({"clave": "clave", "jpeg": True, "h264": False})
        self.assertEqual(gpu.capacidad_vpp(), {"vpp": True, "jpeg": True, "h264": False})
        self.assertEqual(self.medidas, [])

    def test_otro_driver_o_kernel_mide_otra_vez(self):
        self.guardar({"clave": "otra", "jpeg": True, "h264": True})
        self.assertFalse(gpu.capacidad_vpp()["jpeg"])
        self.assertEqual(self.medidas, ["clave"])

    def test_medida_cortada_por_un_cuelgue_veta_la_gpu(self):
        self.guardar({"clave": "clave", "midiendo": True, "arranque": "arranque-0"})
        c = gpu.capacidad_vpp()
        self.assertFalse(c["jpeg"] or c["h264"])
        self.assertEqual(self.medidas, [])
        self.assertEqual(self.eventos, ["gpu_cuelgue"])
        with open(gpu.VPP_ARCHIVO) as fh:
            self.assertEqual(json.load(fh)["error"], "cuelgue")
        # y queda así en los siguientes arranques del servicio
        gpu._VPP.clear()
        self.arranque = "arranque-2"
        self.assertFalse(gpu.capacidad_vpp()["jpeg"])
        self.assertEqual(self.medidas, [])

    def test_medida_cortada_por_reiniciar_el_servicio_se_repite(self):
        self.guardar({"clave": "clave", "midiendo": True, "arranque": "arranque-1"})
        gpu.capacidad_vpp()
        self.assertEqual(self.medidas, ["clave"])
        self.assertEqual(self.eventos, [])

    def test_auto_decodifica_por_gpu_si_compensa(self):
        self.guardar({"clave": "clave", "jpeg": True, "h264": True})
        tmp = tempfile.TemporaryDirectory()
        orig = nucleo.CONFIG
        nucleo.CONFIG = os.path.join(tmp.name, "config.json")
        try:
            sup = supervisor.Fuentes(nucleo.Config())
            f = supervisor.Fuente({"id": "c", "nombre": "C", "dev": "/dev/video9", "usb": "1",
                                   "modos": []})
            self.assertEqual(sup.decodificadores(f, "MJPG"), "gpu")
            self.assertEqual(sup.decodificadores(f, "YUYV"), 1)
            f.deco_gpu_fallida = True       # falló en marcha: vuelta a la CPU
            self.assertEqual(sup.decodificadores(f, "MJPG"), 1)
        finally:
            nucleo.CONFIG = orig
            tmp.cleanup()


class EquipoPotente(unittest.TestCase):
    """Las funciones simultáneas, solo en equipos que dan para ellas."""

    def setUp(self):
        self.orig = (gpu.os.cpu_count, gpu.leer, gpu.capacidad_vpp)
        self.nucleos, self.mem_kb, self.vpp = 4, 8 * 1024 ** 2, {"jpeg": True, "h264": True}
        gpu.os.cpu_count = lambda: self.nucleos
        gpu.leer = lambda ruta, defecto="": f"MemTotal:       {self.mem_kb} kB\n"
        gpu.capacidad_vpp = lambda: {"vpp": True, **self.vpp}

    def tearDown(self):
        gpu.os.cpu_count, gpu.leer, gpu.capacidad_vpp = self.orig

    def test_i3_con_gpu_es_potente(self):
        c = gpu.equipo_potente()
        self.assertTrue(c["potente"])
        self.assertEqual((c["nucleos"], c["memoria_gb"]), (4, 8.0))

    def test_celeron_de_dos_nucleos_no(self):
        self.nucleos = 2
        self.assertFalse(gpu.equipo_potente()["potente"])

    def test_sin_gpu_util_hacen_falta_8_nucleos(self):
        self.vpp = {"jpeg": False, "h264": False}
        self.assertFalse(gpu.equipo_potente()["potente"])
        self.nucleos = 8
        self.assertTrue(gpu.equipo_potente()["potente"])

    def test_poca_memoria_no(self):
        self.mem_kb = 2 * 1024 ** 2
        self.assertFalse(gpu.equipo_potente()["potente"])

    def test_el_ajuste_las_apaga(self):
        self.assertTrue(gpu.simultaneo({}))
        self.assertTrue(gpu.simultaneo({"simultaneo": True}))
        self.assertFalse(gpu.simultaneo({"simultaneo": False}))
        self.nucleos = 2
        self.assertFalse(gpu.simultaneo({"simultaneo": True}))

    def test_cortafuegos_de_receptor_con_camaras(self):
        g = cortafuegos.reglas(["emisor", "receptor"], [9000], [], [22])
        self.assertIn("tcp dport 6960-7059 accept", g)      # recibir
        self.assertRegex(g, r"udp dport \{[^}]*\b9000\b")   # SRT de las cámaras
        solo = cortafuegos.reglas("receptor", [9000], [], [22])
        self.assertNotRegex(solo, r"\b9000\b")


class VariasSalidas(unittest.TestCase):
    """En un equipo potente, cada cámara puede grabar y emitir por SRT y RTMP a la vez."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = (nucleo.CONFIG, supervisor.simultaneo, supervisor.capacidad_gpu,
                     supervisor.capacidad_vpp, cortafuegos.simultaneo)
        nucleo.CONFIG = os.path.join(self.tmp.name, "config.json")
        self.potente = True
        supervisor.simultaneo = cortafuegos.simultaneo = lambda cfg: self.potente
        supervisor.capacidad_gpu = lambda: {"disponible": True, "codificador": "vah264enc"}
        supervisor.capacidad_vpp = lambda: {"vpp": True, "jpeg": True, "h264": True}
        self.sup = supervisor.Fuentes(nucleo.Config())
        self.aj = {"gpu_salida": "srt", "gpu_extra": ["grabar", "rtmp", "srt"], "gpu_kbps": 8000,
                   "srt_puerto": 9000, "srt_latencia": 200, "rtmp_url": "rtmp://x/live/k",
                   "grabar_cuando": "manual", "grabar_desde": "09:00", "grabar_hasta": "18:00"}

    def tearDown(self):
        (nucleo.CONFIG, supervisor.simultaneo, supervisor.capacidad_gpu,
         supervisor.capacidad_vpp, cortafuegos.simultaneo) = self.orig
        self.tmp.cleanup()

    def test_extras_sin_repetir_la_principal(self):
        extra = self.sup.salidas_extra(self.aj, "MJPG")
        self.assertEqual([g["modo"] for g in extra], ["grabar", "rtmp"])
        grabar = extra[0]
        self.assertEqual(grabar["cuando"], "manual")     # su condición, como si fuera la principal
        self.assertEqual(extra[1]["url"], "rtmp://x/live/k")

    def test_en_equipo_justo_solo_la_principal(self):
        self.potente = False
        self.assertEqual(self.sup.salidas_extra(self.aj, "MJPG"), [])

    def test_sin_principal_no_hay_extras(self):
        self.assertEqual(self.sup.salidas_extra({**self.aj, "gpu_salida": "no"}, "MJPG"), [])

    def test_rtmp_sin_url_se_omite(self):
        extra = self.sup.salidas_extra({**self.aj, "rtmp_url": ""}, "MJPG")
        self.assertEqual([g["modo"] for g in extra], ["grabar"])

    def test_cortafuegos_abre_el_srt_adicional(self):
        cf = cortafuegos.Cortafuegos()
        guiones = []
        orig = (cortafuegos.aplicar, cortafuegos.puertos_ssh)
        cortafuegos.aplicar = lambda g: guiones.append(g)
        cortafuegos.puertos_ssh = lambda: [22]
        try:
            cf.actualizar({"modo": "emisor", "cortafuegos": True},
                          [{"gpu_salida": "grabar", "gpu_extra": ["srt"], "srt_puerto": 9100}])
            self.assertRegex(guiones[-1], r"\b9100\b")
            self.potente = False
            cf.actualizar({"modo": "emisor", "cortafuegos": True},
                          [{"gpu_salida": "grabar", "gpu_extra": ["srt"], "srt_puerto": 9100}])
            self.assertNotRegex(guiones[-1], r"\b9100\b")
        finally:
            cortafuegos.aplicar, cortafuegos.puertos_ssh = orig


if __name__ == "__main__":
    unittest.main()
