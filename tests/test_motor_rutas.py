# NDIICAM — pruebas del motor de rutas con procesos de mentira.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
#
#   python3 -m unittest discover -s tests -v
#
# Lo que aquí se prueba es la reconciliación (qué arranca, qué se para, qué se
# ajusta en marcha) y las reglas de validar/plan que salieron de la auditoría. Los
# procesos no existen: un Popen falso que no muere solo y guarda lo que se le escribe.
import json
import os
import queue
import shutil
import sys
import tempfile
import time
import types
import unittest

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(AQUI), "ndiicam"))

# los procesos de GStreamer se importan para probar sus funciones puras: un gi de mentira
if "gi" not in sys.modules:
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **k: None
    repo = types.ModuleType("gi.repository")
    repo.GLib = types.SimpleNamespace()
    repo.Gst = types.SimpleNamespace(SECOND=10 ** 9)
    gi.repository = repo
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repo
if not hasattr(sys.modules["gi.repository"].Gst, "SECOND"):
    sys.modules["gi.repository"].Gst.SECOND = 10 ** 9

import fuente  # noqa: E402
import programa  # noqa: E402
import puente  # noqa: E402
import relevo  # noqa: E402
import rutas  # noqa: E402
import supervisor  # noqa: E402


ENT = [
    {"id": "atem", "tipo": "ingesta", "nombre": "ATEM", "clave": "K1"},
    {"id": "x32", "tipo": "usb_audio", "nombre": "X32", "dev": "plughw:CARD=X32,DEV=0"},
    {"id": "pocket", "tipo": "usb_cam", "nombre": "Pocket", "dev": "/dev/video0",
     "audio_dev": "plughw:CARD=Pocket,DEV=0"},
    {"id": "mview", "tipo": "usb_cam", "nombre": "Multiview", "dev": "/dev/video2"},
    {"id": "obs", "tipo": "ndi", "nombre": "OBS", "fuente": "PC (OBS)"},
]


def ruta(**k):
    r = {"id": "r1", "nombre": "Culto", "video": "atem", "audio": "x32", "ganancia": -3,
         "retraso": 520, "salidas": [{"tipo": "obs"}, {"tipo": "grabar", "formato": "mkv"}]}
    r.update(k)
    return r


def plan(lista, potente=True):
    limpias, err = rutas.validar(lista, ENT)
    return limpias, err, rutas.plan(limpias, ENT, potente, err)


class Validacion(unittest.TestCase):
    """Lo que la auditoría pidió a validar() y plan()."""

    def test_misma_camara_en_dos_rutas(self):
        a = ruta(id="a", nombre="A", video="pocket", audio="", salidas=[{"tipo": "ndi"}])
        b = ruta(id="b", nombre="B", video="pocket", audio="", salidas=[{"tipo": "hdmi"}])
        _, err, p = plan([a, b])
        self.assertEqual(err, {"b": ["camara_ocupada"]})
        self.assertEqual((p["activas"], p["pausadas"]), (["a"], ["b"]))
        self.assertEqual([x["ruta"] for x in p["procesos"]], ["a"])

    def test_una_ruta_apagada_no_ocupa_la_camara(self):
        a = ruta(id="a", nombre="A", video="pocket", audio="", activa=False, salidas=[{"tipo": "ndi"}])
        b = ruta(id="b", nombre="B", video="pocket", audio="", salidas=[{"tipo": "hdmi"}])
        _, err, p = plan([a, b])
        self.assertEqual(err, {})
        self.assertEqual(p["activas"], ["b"])

    def test_mismo_audio_en_dos_rutas_sigue_sin_el(self):
        # la consola en dos rutas: la segunda va, pero sin ese audio (ALSA es de uno solo)
        a = ruta(id="a", nombre="A")
        b = ruta(id="b", nombre="B", video="pocket", salidas=[{"tipo": "ndi"}])
        _, err, p = plan([a, b])
        self.assertEqual(err, {"b": ["audio_ocupado"]})
        self.assertEqual(p["activas"], ["a", "b"])
        cam = next(x for x in p["procesos"] if x["clase"] == "camara")
        self.assertEqual(cam["audio"]["de"], None)

    def test_el_micro_de_una_camara_en_otra_ruta(self):
        a = ruta(id="a", nombre="A", video="pocket", audio="pocket", salidas=[{"tipo": "ndi"}])
        b = ruta(id="b", nombre="B", video="atem", audio="pocket", salidas=[{"tipo": "obs"}])
        _, err, _ = plan([a, b])
        self.assertEqual(err, {"b": ["audio_ocupado"]})

    def test_ndi_repetido_se_renombra(self):
        a = ruta(id="a", nombre="A", video="pocket", audio="", salidas=[{"tipo": "ndi", "nombre": "Culto"}])
        b = ruta(id="b", nombre="B", video="mview", audio="", salidas=[{"tipo": "ndi", "nombre": "culto"}])
        limpias, err, p = plan([a, b])
        self.assertEqual(err, {"b": ["ndi_repetido"]})
        self.assertEqual(limpias[1]["salidas"][0]["nombre"], "culto 2")
        self.assertEqual({x["ndi"] for x in p["procesos"]}, {"Culto", "culto 2"})

    def test_ndi_que_solo_saldria_como_ndi_se_pausa(self):
        a = ruta(id="a", nombre="A", video="obs", audio="", salidas=[{"tipo": "ndi"}])
        b = ruta(id="b", nombre="B", video="pocket", audio="", salidas=[{"tipo": "ndi"}])
        _, err, p = plan([a, b], potente=False)
        self.assertEqual(err["a"], ["ndi_a_ndi"])
        # y no gasta la única plaza de un equipo Lite
        self.assertEqual((p["activas"], p["pausadas"]), (["b"], ["a"]))
        self.assertEqual([x["ruta"] for x in p["procesos"]], ["b"])

    def test_numeros_que_no_lo_son(self):
        limpias, err, _ = plan([ruta(ganancia="x", retraso=None, salidas="no")])
        self.assertEqual((limpias[0]["ganancia"], limpias[0]["retraso"]), (0, 0))
        self.assertEqual(err, {"r1": ["sin_salidas"]})
        limpias, _, _ = plan([ruta(ganancia="6.7", retraso="250")])
        self.assertEqual((limpias[0]["ganancia"], limpias[0]["retraso"]), (6, 250))

    def test_desde_modos_con_dos_camaras_del_mismo_nombre(self):
        cams = {"c1": {"id": "c1", "nombre": "Cam"}, "c2": {"id": "c2", "nombre": "Cam"}}
        aj = {"activa": True, "ndi_nombre": "Cam", "audio": "off", "gpu_salida": "srt", "gpu_extra": ["grabar"]}
        _, lista = rutas.desde_modos({"modo": "emisor", "hdmi": False}, cams, lambda i: aj, [])
        self.assertEqual([r["id"] for r in lista], ["cam-cam", "cam-cam-2"])
        self.assertEqual([s["tipo"] for s in lista[0]["salidas"]], ["ndi", "srt", "grabar"])


class ConfMediaMTX(unittest.TestCase):
    """La configuración no cambia con las rutas: así MediaMTX no se reinicia."""

    def test_estable_al_anadir_rutas(self):
        self.assertEqual(puente.conf_mediamtx_rutas(["live/K1"], ["live/a"]),
                         puente.conf_mediamtx_rutas(["live/K1"], ["live/a", "live/b", "live/c"]))

    def test_permisos(self):
        us = puente.usuarios_rutas(["live/K1", "live/Ab-c"])
        self.assertEqual(us[0]["ips"], ["127.0.0.1", "::1"])
        self.assertEqual(us[1]["permissions"],
                         [{"action": "publish", "path": "~^live/(Ab\\-c|K1)$"}, {"action": "read"}])
        self.assertEqual(puente.usuarios_rutas([])[1]["permissions"], [{"action": "read"}])
        conf = puente.conf_mediamtx_rutas(["live/K1"])
        self.assertIn("'~^live/.*$': {}", conf)
        self.assertIn("path: '~^live/(K1)$'", conf)

    def test_cambio_de_permisos_por_la_api_sin_reiniciar(self):
        s = puente.ServidorPuente()
        proc = FalsoProc()
        s.proc, s.conf = proc, "antes"
        llamadas = []
        s._api = lambda ruta, datos=None: llamadas.append((ruta, datos)) or {}
        s.arrancar_conf("despues", [{"user": "any"}])
        self.assertIs(s.proc, proc)                      # sigue el mismo MediaMTX
        self.assertEqual(llamadas, [("/v3/config/global/patch", {"authInternalUsers": [{"user": "any"}]})])
        self.assertEqual(s.conf, "despues")

    def test_una_sola_consulta_para_todos_los_caminos(self):
        s = puente.ServidorPuente()
        s.proc = FalsoProc()
        pedidas = []

        def api(ruta, datos=None):
            pedidas.append(ruta)
            return {"items": [{"name": "live/x", "ready": True, "tracks": ["H264", "MPEG-4 Audio"],
                               "readers": [1, 2]}]}
        s._api = api
        self.assertTrue(s.ruta_lista("live/x"))
        self.assertEqual(s.ruta_info("live/x")["lectores"], 2)
        self.assertFalse(s.ruta_lista("live/otro"))     # no publica: sin consultarlo aparte
        self.assertEqual(len(pedidas), 1)
        self.assertTrue(pedidas[0].startswith("/v3/paths/list"))


class FalsoProc:
    """Un Popen que no muere hasta que se le mata y guarda lo que se le escribe."""

    def __init__(self):
        self.escrito = []
        self.vivo = True
        self.terminado = False
        self._cola = queue.Queue()
        self.stdin = self
        self.stdout = self
        self.pid = 4242

    def write(self, s):
        self.escrito.append(s)

    def flush(self):
        pass

    def close(self):
        pass

    def __iter__(self):
        return self

    def __next__(self):
        # el hilo lector se queda esperando mientras el proceso "viva"
        self._cola.get()
        raise StopIteration

    def poll(self):
        return None if self.vivo else 0

    def terminate(self):
        self.terminado = True
        self.vivo = False
        self._cola.put(None)

    kill = terminate

    def wait(self, timeout=None):
        return 0

    def ordenes(self):
        return [json.loads(x) for x in self.escrito[1:]]     # la primera línea es la configuración


class FalsoPuente:
    def __init__(self):
        self.publicando = {}        # camino -> pistas (None: nadie publica)

    def disponible(self):
        return False

    def detener(self):
        pass

    def ruta_info(self, camino):
        pistas = self.publicando.get(camino)
        if pistas is None:
            return {"activo": True, "publicando": False}
        return {"activo": True, "publicando": True, "pistas": pistas, "lectores": 1}

    def ruta_lista(self, camino):
        return self.publicando.get(camino) is not None


class FalsoSup:
    """Lo que el motor usa de Fuentes: la configuración de cada cámara, las
    interfaces de audio y objetivo()."""

    def __init__(self):
        # la consola por USB y el micro de la Pocket (que comparte USB con la cámara)
        self.audio = [{"id": "X32", "nombre": "X32", "dev": "plughw:CARD=X32,DEV=0", "usb": None},
                      {"id": "Pocket", "nombre": "Pocket", "dev": "plughw:CARD=Pocket,DEV=0", "usb": "usb1"}]
        self.config = types.SimpleNamespace(fuente=lambda i: {"audio": "auto", "gpu_kbps": 8000})

    def objetivo(self, f, aj):
        gpu = {"modo": "rtmp", "url": aj["rtmp_url"], "kbps": 8000} if aj["gpu_salida"] == "rtmp" else None
        return {"dev": f.cam["dev"], "ancho": 1920, "alto": 1080, "fps": 30, "fmt": "MJPG",
                "nombre": f.cam["nombre"], "audio": None, "retraso_audio": 0, "baja_latencia": False,
                "decodificadores": "gpu", "hdmi": None, "gpu": gpu, "gpu_extra": [], "solo_monitor": False}


CAMS = {"pocket": {"id": "pocket", "nombre": "Pocket", "dev": "/dev/video0", "usb": "usb1", "modos": []},
        "mview": {"id": "mview", "nombre": "Multiview", "dev": "/dev/video2", "usb": None, "modos": []}}
MONITOR = {"id": 95, "conector": "HDMI-A-1", "conectado": True, "modo": "1920x1080"}


class Motor(unittest.TestCase):
    """La reconciliación de MotorRutas con procesos y MediaMTX de mentira."""

    def setUp(self):
        self.orig = (supervisor.lanzar, supervisor.PUENTE, supervisor.evento, supervisor.simultaneo,
                     supervisor.ruta_preview, supervisor.audio_hdmi, supervisor.resolucion_pantalla,
                     supervisor.decodificador_h264, supervisor.capacidad_vpp, supervisor.capacidad_gpu)
        self.lanzados = []

        def lanzar(script, obj, env=None):
            p = FalsoProc()
            p.script, p.obj = os.path.basename(script), obj
            p.write(json.dumps(obj) + "\n")
            self.lanzados.append(p)
            return p
        supervisor.lanzar = lanzar
        self.puente = supervisor.PUENTE = FalsoPuente()
        supervisor.evento = lambda *a, **k: None
        supervisor.simultaneo = lambda cfg: True
        supervisor.ruta_preview = lambda i: f"/tmp/prev-{i}.jpg"
        supervisor.audio_hdmi = lambda: "hdmi"
        supervisor.resolucion_pantalla = lambda m: [1920, 1080]
        supervisor.decodificador_h264 = lambda: "avdec_h264"
        supervisor.capacidad_vpp = lambda: {"h264": True, "jpeg": True, "vpp": True}
        supervisor.capacidad_gpu = lambda: {"disponible": True, "codificador": "vah264lpenc"}
        self.motor = supervisor.MotorRutas(FalsoSup())
        self.cfg = {"entradas_red": [{"id": "atem", "tipo": "ingesta", "nombre": "ATEM", "clave": "K1"}],
                    "rutas": []}

    def tearDown(self):
        (supervisor.lanzar, supervisor.PUENTE, supervisor.evento, supervisor.simultaneo,
         supervisor.ruta_preview, supervisor.audio_hdmi, supervisor.resolucion_pantalla,
         supervisor.decodificador_h264, supervisor.capacidad_vpp, supervisor.capacidad_gpu) = self.orig

    def paso(self, rutas_cfg, monitor=MONITOR):
        self.cfg["rutas"] = rutas_cfg
        self.motor.paso(self.cfg, CAMS, monitor)
        return self.motor.procs

    def test_relevo_espera_a_que_haya_programa_y_luego_no_se_mata(self):
        r = ruta(audio="", salidas=[{"tipo": "obs"}, {"tipo": "grabar", "formato": "mkv"}])
        procs = self.paso([r])
        self.assertEqual(procs, {})                 # el ATEM aún no publica: nada que arrancar
        self.assertEqual(self.motor.estado()["r1"]["estado"], "esperando")
        self.puente.publicando = {"live/K1": ["H264", "MPEG-4 Audio"]}
        procs = self.paso([r])
        self.assertEqual(sorted(procs), ["prog:r1"])      # el programa sí; el relevo, cuando haya programa
        prog = procs["prog:r1"].proc
        self.assertEqual(prog.obj["con_audio"], True)
        self.puente.publicando["live/culto"] = ["H264", "MPEG-4 Audio"]
        procs = self.paso([r])
        self.assertEqual(sorted(procs), ["prog:r1", "relevo:r1:r1-grabar"])
        relevo = procs["relevo:r1:r1-grabar"].proc
        self.assertEqual(relevo.obj["modo"], "grabar")
        # MediaMTX deja de contestar (o el ATEM parpadea): lo que corre sigue corriendo
        self.puente.publicando = {}
        procs = self.paso([r])
        self.assertIs(procs["prog:r1"].proc, prog)
        self.assertIs(procs["relevo:r1:r1-grabar"].proc, relevo)
        self.assertFalse(prog.terminado or relevo.terminado)

    def test_sin_audio_en_la_ingesta(self):
        self.puente.publicando = {"live/K1": ["H264"], "live/culto": ["H264"]}
        r = ruta(audio="", salidas=[{"tipo": "grabar"}])
        procs = self.paso([r])
        self.assertEqual(procs["prog:r1"].proc.obj["con_audio"], False)
        self.assertEqual(procs["relevo:r1:r1-grabar"].proc.obj["con_audio"], False)

    def test_ajustes_en_marcha_por_stdin_sin_reiniciar(self):
        self.puente.publicando = {"live/K1": ["H264", "MPEG-4 Audio"]}
        r = ruta(audio="audio:X32", salidas=[{"tipo": "obs"}])
        procs = self.paso([r])
        prog = procs["prog:r1"].proc
        self.assertEqual((prog.obj["retraso_audio"], prog.obj["audio"]), (520, "plughw:CARD=X32,DEV=0"))
        procs = self.paso([{**r, "retraso": 480, "mudo": True}])
        self.assertIs(procs["prog:r1"].proc, prog)
        self.assertEqual(prog.ordenes(), [{"retraso_audio": 480, "mudo": True}])
        # cambiar el audio (la consola por el del ATEM) sí reinicia
        procs = self.paso([{**r, "audio": "atem"}])
        self.assertTrue(prog.terminado)

    def test_camara_respeta_hdmi_y_audio_fallidos(self):
        r = ruta(video="pocket", audio="pocket", salidas=[{"tipo": "ndi"}, {"tipo": "hdmi"}])
        procs = self.paso([r])
        f = procs["cam:r1"]
        self.assertEqual((f.proc.obj["hdmi"], f.proc.obj["audio"]), (95, "plughw:CARD=Pocket,DEV=0"))
        f.hdmi_fallido, f.audio_fallido = True, "plughw:CARD=Pocket,DEV=0"
        f.estado = "emitiendo"
        viejo = f.proc
        procs = self.paso([r])
        self.assertTrue(viejo.terminado)            # se reinicia sin HDMI ni ese audio...
        self.assertEqual((f.proc.obj["hdmi"], f.proc.obj["audio"]), (None, None))
        self.assertEqual(self.motor.errores, {"r1": ["audio_fallido", "hdmi_fallido"]})
        f.estado = "emitiendo"
        est = self.motor.estado()["r1"]
        self.assertEqual(est["estado"], "en_marcha")
        self.assertEqual(est["salidas"]["r1-hdmi"], "error")   # ...y el panel ve qué salida falló
        procs = self.paso([r])
        self.assertIs(procs["cam:r1"].proc, f.proc)  # y no entra en bucle

    def test_salida_de_red_a_una_camara_sin_reiniciarla(self):
        r = ruta(video="pocket", audio="", salidas=[{"tipo": "ndi"}])
        procs = self.paso([r])
        f = procs["cam:r1"]
        proc = f.proc
        self.assertIsNone(proc.obj["gpu"])
        procs = self.paso([{**r, "salidas": [{"tipo": "ndi"}, {"tipo": "obs"}]}])
        self.assertIs(f.proc, proc)
        self.assertEqual(proc.ordenes(), [{"gpu": {"modo": "rtmp", "url": "rtmp://127.0.0.1:1935/live/culto",
                                                   "kbps": 8000}}])
        self.assertEqual(f.gpu["estado"], "iniciando")
        procs = self.paso([r])
        self.assertEqual(proc.ordenes()[-1], {"gpu": None})

    def test_reiniciar_desde_el_panel(self):
        r = ruta(video="pocket", audio="", salidas=[{"tipo": "ndi"}])
        f = self.paso([r])["cam:r1"]
        viejo = f.proc
        f.hdmi_fallido = True
        self.motor.reiniciar("pocket")
        self.paso([r])
        self.assertTrue(viejo.terminado)
        self.assertIsNot(f.proc, viejo)
        self.assertFalse(f.hdmi_fallido)

    def test_un_relevo_caido_no_tumba_la_ruta(self):
        self.puente.publicando = {"live/K1": ["H264"], "live/culto": ["H264"]}
        r = ruta(audio="", salidas=[{"tipo": "obs"}, {"tipo": "rtmp", "url": "rtmp://a.rtmp.youtube.com/live2/x"}])
        procs = self.paso([r])
        procs["prog:r1"].estado = "activa"
        procs["relevo:r1:r1-rtmp"].estado = "error"
        est = self.motor.estado()["r1"]
        self.assertEqual(est["estado"], "en_marcha")
        self.assertEqual(est["salidas"], {"r1-obs": "ok", "r1-rtmp": "error"})

    def test_la_pantalla_de_ayuda_suelta_el_monitor_antes(self):
        soltado = []
        self.motor.soltar_hdmi = lambda: soltado.append(True)
        r = ruta(video="mview", audio="", salidas=[{"tipo": "hdmi"}])
        self.paso([r])
        self.assertEqual(soltado, [True])
        self.assertTrue(self.motor.hdmi)
        self.paso([])
        self.assertFalse(self.motor.hdmi)

    def test_quitar_una_ruta_para_sus_procesos(self):
        r = ruta(video="pocket", audio="", salidas=[{"tipo": "ndi"}])
        proc = self.paso([r])["cam:r1"].proc
        self.assertEqual(self.paso([]), {})
        self.assertTrue(proc.terminado)


class Procesos(unittest.TestCase):
    """Las descripciones de los pipelines y la limpieza del disco."""

    def test_relevo_sin_audio_no_espera_una_pista_que_no_llega(self):
        con = relevo.descripcion({"desde": "x", "modo": "grabar", "formato": "mkv"})
        sin = relevo.descripcion({"desde": "x", "modo": "grabar", "formato": "mkv", "con_audio": False})
        self.assertIn("aacparse name=ha ! rec.audio_0", con)
        self.assertNotIn("d.audio", sin)
        self.assertNotIn("location=", sin)              # la dirección va por set_property
        self.assertIn("matroskamux", sin)
        yt = relevo.descripcion({"desde": "x", "modo": "rtmp", "url": "u", "con_audio": True})
        self.assertIn("m.audio", yt)
        self.assertNotIn("location=", yt)

    def test_programa_siempre_con_pista_de_audio(self):
        consola = programa.descripcion({"audio": "plughw:X32"})
        self.assertIn("alsasrc name=asrc", consola)
        self.assertNotIn("plughw", consola)             # el dispositivo, por set_property
        atem = programa.descripcion({"audio": None, "con_audio": True})
        self.assertIn("avdec_aac", atem)
        nada = programa.descripcion({"audio": None, "con_audio": False})
        self.assertIn("audiotestsrc name=sil wave=silence", nada)
        self.assertIn("d.audio ! queue ! fakesink", nada)
        for d in (consola, atem, nada):
            self.assertIn("ta. !", d)
            self.assertNotIn("location=", d)

    def test_reloj_converge_rapido_al_arrancar_y_despacio_despues(self):
        pad = types.SimpleNamespace(offsets=[], set_offset=lambda ns: pad.offsets.append(ns))
        r = programa.Reloj(None, pad)
        r.inicio = 0.0
        r._fijar(1.5)               # el primer fotograma, sesgado por la ráfaga inicial
        r.muestras = [(0.5, 1.5), (1.0, 0.7), (2.0, 0.72)]
        r.corregir()
        self.assertAlmostEqual(r.offset, 0.7)           # de golpe, los primeros segundos
        r.muestras = [(20.0, 0.65), (25.0, 0.66)]
        r.corregir()
        self.assertAlmostEqual(r.offset, 0.698)         # después, 2 ms por segundo
        r.muestras = [(30.0, 3.0)]
        r.corregir()
        self.assertAlmostEqual(r.offset, 3.0)           # un salto de más de un segundo, de golpe

    def test_liberar_disco_borra_tambien_los_mkv(self):
        d = tempfile.mkdtemp()
        try:
            for i, nombre in enumerate(["a.mkv", "b.mp4", "c.mkv", "d.txt"]):
                with open(os.path.join(d, nombre), "w") as f:
                    f.write("x")
                os.utime(os.path.join(d, nombre), (1000 + i, 1000 + i))
            orig = fuente.shutil.disk_usage
            llamadas = [0]

            def uso(_d):
                llamadas[0] += 1
                return types.SimpleNamespace(free=0 if llamadas[0] <= 2 else 10 ** 12)
            fuente.shutil.disk_usage = uso
            try:
                fuente.liberar_disco(d, kbps=20000)
            finally:
                fuente.shutil.disk_usage = orig
            self.assertEqual(sorted(os.listdir(d)), ["c.mkv", "d.txt"])   # los dos más viejos, fuera
        finally:
            shutil.rmtree(d)

    def test_reserva_segun_la_tasa(self):
        # a 20 Mbit/s, dos ficheros de 10 min son 3 GB: más que el mínimo de 2; a
        # 4 Mbit/s manda el mínimo; sin tasa (una copia del ATEM), 4 GB
        d = tempfile.mkdtemp()
        orig_uso, orig_rm = shutil.disk_usage, os.remove     # fuente.os y fuente.shutil son estos
        reservas = []
        try:
            with open(os.path.join(d, "v.mkv"), "w") as f:
                f.write("x")
            shutil.disk_usage = lambda _d: types.SimpleNamespace(free=2_999_999_999)
            for kbps in (20000, 4000, None):
                borrado = []
                os.remove = lambda p: borrado.append(p)
                fuente.liberar_disco(d, kbps=kbps)
                reservas.append(bool(borrado))
        finally:
            shutil.disk_usage, os.remove = orig_uso, orig_rm
            shutil.rmtree(d)
        self.assertEqual(reservas, [True, False, True])


class Terminar(unittest.TestCase):
    def test_terminar_no_bloquea_y_cierra_stdin(self):
        p = FalsoProc()
        cerrado = []
        p.close = lambda: cerrado.append(True)
        t = time.monotonic()
        self.assertIs(supervisor.terminar(p, 8), p)
        self.assertLess(time.monotonic() - t, 0.5)
        self.assertTrue(p.terminado)
        self.assertEqual(cerrado, [True])
        self.assertIsNone(supervisor.terminar(None))
        supervisor.esperar_fin([p, None], 0.1)


if __name__ == "__main__":
    unittest.main()


class AuxiliarColgado(unittest.TestCase):
    """Un auxiliar que sigue informando pero no saca fotogramas se reinicia."""

    def medir_serie(self, outs):
        a = supervisor.Auxiliar("relevo.py", "relevo")
        fallos = []
        a._fallo = lambda e: fallos.append(e)
        a.proc, a.obj, a.estado = object(), {"nombre": "Culto"}, "activa"
        for i, out in enumerate(outs):
            a.c = {"t": i * 2.0, "out": out, "bytes": 0, "aud": 0}
            a.medir(i * 2.0)
            if fallos:
                break
        return fallos

    def test_sin_avance_reinicia(self):
        self.assertEqual(self.medir_serie([100, 130] + [130] * 8), ["sin_senal"])

    def test_avanzando_no_reinicia(self):
        self.assertEqual(self.medir_serie([i * 30 for i in range(15)]), [])

    def test_parada_corta_no_reinicia(self):
        self.assertEqual(self.medir_serie([0, 30, 60, 60, 60, 90, 120]), [])


class FuenteSinImagen(unittest.TestCase):
    """Una fuente anunciada que no manda imagen se explica en la ruta."""

    def test_receptor_sin_senal_da_aviso(self):
        m = supervisor.MotorRutas.__new__(supervisor.MotorRutas)
        rx = types.SimpleNamespace(fallos=2, ultimo_error="sin_senal", stats={}, proc=None, estado="error")
        m.procs = {"rx:r1": rx}
        m.errores, m.ultimo = {}, {"pausadas": []}
        m.limpias = [{"id": "r1", "camino": "r1", "salidas": [{"id": "s1", "tipo": "hdmi"}]}]
        self.assertIn("fuente_sin_imagen", m.estado()["r1"]["errores"])

    def test_sin_fallos_no_avisa(self):
        m = supervisor.MotorRutas.__new__(supervisor.MotorRutas)
        rx = types.SimpleNamespace(fallos=0, ultimo_error="sin_senal", stats={}, proc=None, estado="conectando")
        m.procs = {"rx:r1": rx}
        m.errores, m.ultimo = {}, {"pausadas": []}
        m.limpias = [{"id": "r1", "camino": "r1", "salidas": [{"id": "s1", "tipo": "hdmi"}]}]
        self.assertNotIn("fuente_sin_imagen", m.estado()["r1"]["errores"])
