# NDIICAM — pruebas de pantallas.py y del reparto de monitores entre rutas.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Lo que se puede probar sin tarjeta: elegir controlador y modo, repartir las
pantallas entre las rutas, y que lanzar() pase el alquiler al hijo."""
import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ndiicam"))
sys.modules.setdefault("gi", types.SimpleNamespace(require_version=lambda *a: None,
                                                   repository=types.SimpleNamespace(Gst=None, GLib=None)))

import nucleo  # noqa: E402
import pantallas  # noqa: E402
import rutas  # noqa: E402
import supervisor  # noqa: E402

CON = [{"id": 95, "nombre": "DP-1", "conectado": True, "crtc": 0, "posibles": [51, 72, 93]},
       {"id": 112, "nombre": "DP-2", "conectado": True, "crtc": 51, "posibles": [51, 72, 93]}]


class Controlador(unittest.TestCase):
    def test_la_pantalla_con_controlador_se_lo_queda(self):
        self.assertEqual(pantallas.elegir_crtc(CON[1], CON, [51, 72, 93]), 51)

    def test_la_nueva_coge_uno_libre_que_no_sea_de_otra(self):
        self.assertEqual(pantallas.elegir_crtc(CON[0], CON, [72, 93]), 72)
        self.assertEqual(pantallas.elegir_crtc(CON[0], CON, [51, 93]), 93)   # el 51 es de DP-2

    def test_sin_controlador_libre(self):
        self.assertIsNone(pantallas.elegir_crtc(CON[0], CON, [51]))

    def test_modo(self):
        modos = [{"ancho": 1920, "alto": 1080, "preferido": False, "indice": 0},
                 {"ancho": 2560, "alto": 1440, "preferido": True, "indice": 1}]
        self.assertEqual(pantallas.elegir_modo(modos)["ancho"], 2560)
        self.assertEqual(pantallas.elegir_modo(modos, (1920, 1080))["indice"], 0)
        self.assertEqual(pantallas.elegir_modo(modos, (800, 600))["ancho"], 2560)   # no está: el preferido
        self.assertIsNone(pantallas.elegir_modo([]))


ENT = [{"id": "pocket", "tipo": "usb_cam", "nombre": "Pocket", "dev": "/dev/video0", "audio_dev": None},
       {"id": "mview", "tipo": "usb_cam", "nombre": "Multiview", "dev": "/dev/video2", "audio_dev": None},
       {"id": "obs", "tipo": "ndi", "nombre": "OBS", "fuente": "PC (OBS)"}]


def ruta(**k):
    r = {"id": "r1", "nombre": "R", "video": "pocket", "audio": "", "salidas": [{"tipo": "hdmi"}]}
    r.update(k)
    return r


def plan(lista, pants):
    limpias, err = rutas.validar(lista, ENT)
    p = rutas.plan(limpias, ENT, True, err, pants)
    return err, {x["ruta"]: x for x in p["procesos"]}, p


class Reparto(unittest.TestCase):
    """Una ruta por pantalla; en "auto", la primera libre; la pedida, si está."""

    def test_dos_rutas_dos_pantallas(self):
        err, procs, p = plan([ruta(id="a", video="pocket"), ruta(id="b", video="mview")], ["DP-2", "DP-1"])
        self.assertEqual(err, {})
        self.assertEqual((procs["a"]["hdmi"], procs["b"]["hdmi"]), ("DP-2", "DP-1"))

    def test_pedida(self):
        err, procs, _ = plan([ruta(id="a", video="pocket", salidas=[{"tipo": "hdmi", "pantalla": "DP-1"}]),
                              ruta(id="b", video="mview")], ["DP-2", "DP-1"])
        self.assertEqual(err, {})
        self.assertEqual((procs["a"]["hdmi"], procs["b"]["hdmi"]), ("DP-1", "DP-2"))

    def test_pedida_que_no_esta(self):
        err, procs, p = plan([ruta(id="a", salidas=[{"tipo": "hdmi", "pantalla": "HDMI-A-1"}, {"tipo": "ndi"}])],
                             ["DP-2"])
        self.assertEqual(err, {"a": ["pantalla_no_esta"]})
        self.assertFalse(procs["a"]["hdmi"])
        self.assertEqual(procs["a"]["ndi"], "R")       # lo demás de la ruta sigue

    def test_tres_rutas_dos_pantallas(self):
        err, procs, p = plan([ruta(id="a", video="pocket"), ruta(id="b", video="mview"),
                              ruta(id="c", video="obs")], ["DP-2", "DP-1"])
        self.assertEqual(err, {"c": ["hdmi_ocupado"]})
        self.assertEqual(p["pausadas"], ["c"])         # su único destino era el monitor
        self.assertNotIn("c", procs)

    def test_sin_monitor(self):
        err, procs, p = plan([ruta(id="a", salidas=[{"tipo": "hdmi"}, {"tipo": "ndi"}])], [])
        self.assertEqual(err, {"a": ["sin_monitor"]})
        self.assertFalse(procs["a"]["hdmi"])

    def test_pantalla_mal_escrita_es_auto(self):
        limpias, _ = rutas.validar([ruta(salidas=[{"tipo": "hdmi", "pantalla": "DP 2;rm"}])], ENT)
        self.assertEqual(limpias[0]["salidas"][0]["pantalla"], "auto")

    def test_sin_lista_un_monitor_sin_nombre(self):
        err, procs, _ = plan([ruta(id="a", video="pocket"), ruta(id="b", video="mview")], None)
        self.assertEqual((procs["a"]["hdmi"], err), (True, {"b": ["hdmi_ocupado"]}))


class Alquiler(unittest.TestCase):
    """lanzar() pide la pantalla a la tarjeta y se la pasa al hijo."""

    def setUp(self):
        self.orig = (supervisor.subprocess.Popen, nucleo.PANTALLAS, supervisor.os.close)
        self.lanzado = {}

        def popen(args, **k):
            self.lanzado.update(k)
            p = types.SimpleNamespace(stdin=types.SimpleNamespace(write=lambda s: self.lanzado.update(cfg=json.loads(s)),
                                                                  flush=lambda: None))
            return p
        supervisor.subprocess.Popen = popen
        self.cerrados = []
        supervisor.os.close = lambda fd: self.cerrados.append(fd)

    def tearDown(self):
        supervisor.subprocess.Popen, nucleo.PANTALLAS, supervisor.os.close = self.orig

    def test_con_alquiler(self):
        nucleo.PANTALLAS = types.SimpleNamespace(alquilar=lambda cid: 7 if cid == 112 else None)
        supervisor.lanzar("x.py", {"nombre": "a", "hdmi": 112})
        self.assertEqual(self.lanzado["pass_fds"], (7,))
        self.assertEqual(self.lanzado["cfg"]["drm_fd"], 7)
        self.assertEqual(self.cerrados, [7])        # la copia del padre se cierra

    def test_sin_alquiler_abre_el_hijo(self):
        nucleo.PANTALLAS = types.SimpleNamespace(alquilar=lambda cid: None)
        supervisor.lanzar("x.py", {"nombre": "a", "hdmi": 112})
        self.assertEqual(self.lanzado["pass_fds"], ())
        self.assertNotIn("drm_fd", self.lanzado["cfg"])

    def test_sin_monitor_no_pide_nada(self):
        pedidos = []
        nucleo.PANTALLAS = types.SimpleNamespace(alquilar=lambda cid: pedidos.append(cid))
        supervisor.lanzar("x.py", {"nombre": "a", "hdmi": None})
        self.assertEqual(pedidos, [])


if __name__ == "__main__":
    unittest.main()
