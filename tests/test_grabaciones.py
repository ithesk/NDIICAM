# NDIICAM — pruebas de grabaciones.py (sesiones, marcas, disco, USB).
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Todo sobre una carpeta temporal: nada de GStreamer ni de discos reales. El
espacio libre se simula parcheando shutil.disk_usage."""
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(AQUI), "ndiicam"))

# fuente.py (por su liberar_disco) importa GStreamer: un gi de mentira, como en los demás tests
if "gi" not in sys.modules:
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **k: None
    repo = types.ModuleType("gi.repository")
    repo.GLib = types.SimpleNamespace()
    repo.Gst = types.SimpleNamespace(SECOND=10 ** 9)
    gi.repository = repo
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repo

import grabaciones as g  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._dir = g.DIR
        g.DIR = self.dir
        g._uniendo.clear()
        g._miniaturas.clear()

    def tearDown(self):
        g.DIR = self._dir
        shutil.rmtree(self.dir, ignore_errors=True)

    def escribir(self, sesion, fichero, kb, mtime=None):
        ruta = os.path.join(self.dir, sesion, fichero) if sesion else os.path.join(self.dir, fichero)
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        with open(ruta, "wb") as f:
            f.write(b"x" * kb * 1024)
        if mtime is not None:
            os.utime(ruta, (mtime, mtime))
        return ruta


class Nombres(Base):
    def test_slug(self):
        self.assertEqual(g.slug("Culto "), "Culto")
        self.assertEqual(g.slug("Nueva ruta 2"), "Nueva-ruta-2")
        self.assertEqual(g.slug(""), "ruta")
        self.assertEqual(g.slug("¡Cámara / A!"), "Cámara-A")

    def test_nombre_sesion(self):
        t = time.mktime((2026, 10, 25, 11, 0, 0, 0, 0, -1))
        self.assertEqual(g.nombre_sesion("Culto", t), "Culto_2026-10-25_11-00-00")

    def test_reserva(self):
        self.assertEqual(g.reserva_disco(), g.RESERVA_SIN_TASA)
        self.assertEqual(g.reserva_disco(kbps=25000), max(g.RESERVA_MINIMA, 2 * 600 * 25000 * 1000 // 8))
        self.assertEqual(g.reserva_disco(reserva=123), 123)


class Sesiones(Base):
    def test_abrir_trozos_cerrar(self):
        t0 = 1_700_000_000.0
        s = g.abrir_sesion("Culto", "r1", "mkv", t0)
        self.assertEqual(s, g.nombre_sesion("Culto", t0))
        f1 = g.nuevo_trozo(s, t0)
        self.assertEqual(os.path.basename(f1), "trozo-01.mkv")
        self.escribir(s, "trozo-01.mkv", 10)
        f2 = g.nuevo_trozo(s, t0 + 600)
        self.assertEqual(os.path.basename(f2), "trozo-02.mkv")
        self.escribir(s, "trozo-02.mkv", 5)
        g.cerrar_sesion(s, t0 + 900)
        d = json.load(open(os.path.join(self.dir, s, "sesion.json")))
        self.assertEqual((d["ruta"], d["ruta_nombre"], d["formato"], d["inicio"], d["fin"]), ("r1", "Culto", "mkv", t0, t0 + 900))
        self.assertEqual([(t["inicio"], t["fin"], t["bytes"]) for t in d["trozos"]],
                         [(t0, t0 + 600, 10 * 1024), (t0 + 600, t0 + 900, 5 * 1024)])
        self.assertEqual((d["marcas"], d["protegida"]), ([], False))

    def test_dos_sesiones_en_el_mismo_segundo(self):
        t0 = 1_700_000_000.0
        a = g.abrir_sesion("Culto", "r1", "mp4", t0)
        b = g.abrir_sesion("Culto", "r1", "mp4", t0)
        self.assertEqual(b, a + "-2")

    def test_nuevo_trozo_invalida_la_completa(self):
        s = g.abrir_sesion("Culto", "r1", "mp4", 1_700_000_000.0)
        g.nuevo_trozo(s)
        self.escribir(s, "completa.mp4", 1)
        g.nuevo_trozo(s)
        self.assertFalse(os.path.exists(os.path.join(self.dir, s, "completa.mp4")))

    def test_listar(self):
        t0 = time.time() - 1000
        viva = g.abrir_sesion("Culto", "r1", "mkv", t0)
        g.nuevo_trozo(viva, t0); self.escribir(viva, "trozo-01.mkv", 10)
        g.nuevo_trozo(viva, t0 + 600); self.escribir(viva, "trozo-02.mkv", 4)
        cortada = g.abrir_sesion("Otra", "r2", "mp4", t0 - 5000)
        g.nuevo_trozo(cortada, t0 - 5000); self.escribir(cortada, "trozo-01.mp4", 2)
        cerrada = g.abrir_sesion("Vieja", "r3", "mp4", t0 - 9000)
        g.nuevo_trozo(cerrada, t0 - 9000); self.escribir(cerrada, "trozo-01.mp4", 3)
        g.cerrar_sesion(cerrada, t0 - 8400)
        g.proteger(cerrada, True)
        self.escribir(None, "suelto_2026.mp4", 7, mtime=t0 - 20000)
        with mock.patch.object(g.shutil, "disk_usage", return_value=types.SimpleNamespace(free=100 * 1024 ** 3)):
            r = g.listar(vivas=[viva])
        self.assertEqual([s["nombre"] for s in r["sesiones"]], [viva, cortada, cerrada])
        v, c, z = r["sesiones"]
        self.assertTrue(v["grabando"] and not v["cortada"])
        self.assertTrue(v["trozos"][1]["grabando"] and not v["trozos"][0]["grabando"])
        self.assertAlmostEqual(v["duracion"], 1000, delta=5)       # el trozo abierto cuenta hasta ahora
        self.assertEqual(v["bytes"], 14 * 1024)
        self.assertTrue(c["cortada"] and not c["grabando"])
        self.assertEqual(c["duracion"], 0)                          # cortada sin fin: no se sabe
        self.assertEqual((z["protegida"], z["duracion"], z["fin"]), (True, 600, t0 - 8400))
        self.assertEqual(z["completa"]["estado"], "no")
        self.assertFalse(z["miniatura"])
        self.assertEqual([x["nombre"] for x in r["sueltos"]], ["suelto_2026.mp4"])
        self.assertEqual(r["reserva"], g.RESERVA_SIN_TASA)
        self.assertFalse(r["aviso_disco"])
        with mock.patch.object(g.shutil, "disk_usage", return_value=types.SimpleNamespace(free=10 * 1024 ** 3)):
            self.assertTrue(g.listar()["aviso_disco"])

    def test_completa_lista_y_preparando(self):
        s = g.abrir_sesion("Culto", "r1", "mp4", time.time() - 100)
        g.nuevo_trozo(s); self.escribir(s, "trozo-01.mp4", 2)
        self.escribir(s, "completa.mp4.tmp", 1)
        self.assertEqual(g.estado_union(s)["estado"], "preparando")
        os.remove(os.path.join(self.dir, s, "completa.mp4.tmp"))
        self.escribir(s, "completa.mp4", 3)
        self.assertEqual(g.estado_union(s), {"estado": "lista", "fichero": "completa.mp4", "bytes": 3 * 1024})

    def test_unir_no_repite_si_esta_al_dia(self):
        s = g.abrir_sesion("Culto", "r1", "mp4", time.time() - 100)
        g.nuevo_trozo(s); self.escribir(s, "trozo-01.mp4", 2, mtime=time.time() - 50)
        self.escribir(s, "completa.mp4", 3)
        with mock.patch.object(g.subprocess, "Popen") as popen:
            self.assertTrue(g.unir(s))
            popen.assert_not_called()
            os.utime(os.path.join(self.dir, s, "trozo-01.mp4"), None)   # trozo más nuevo que la completa
            self.assertTrue(g.unir(s))
            popen.assert_called_once()
            self.assertEqual(popen.call_args[0][0][1:], [g.UNIR_PY, "unir", os.path.join(self.dir, s), "mp4"])

    def test_borrar(self):
        s = g.abrir_sesion("Culto", "r1", "mp4", 1_700_000_000.0)
        self.escribir(None, "suelto.mp4", 1)
        self.assertTrue(g.borrar(s) and not os.path.exists(os.path.join(self.dir, s)))
        self.assertTrue(g.borrar("suelto.mp4"))
        self.assertFalse(g.borrar("../otra"))
        self.assertFalse(g.borrar("no-existe"))


class Marcas(Base):
    def test_marca_y_capitulos(self):
        t0 = time.time() - 3600
        s = g.abrir_sesion("Culto", "r1", "mp4", t0)
        m = g.marca(s, "Sermón", 754)
        self.assertEqual((m["t"], m["hora"], m["texto"]), (754, "00:12:34", "Sermón"))
        g.marca(s, "Alabanza", 120)
        m3 = g.marca(s)                                  # ahora: ~3600 s
        self.assertAlmostEqual(m3["t"], 3600, delta=5)
        self.assertEqual(g.marcas_txt(s), "00:00:00 Inicio\n00:02:00 Alabanza\n00:12:34 Sermón\n01:00:00 Marca\n")
        g.marca(s, "Inicio del culto", 0)
        self.assertTrue(g.marcas_txt(s).startswith("00:00:00 Inicio del culto\n00:02:00"))


class Disco(Base):
    def test_liberar_lo_viejo_sin_tocar_protegidas_ni_el_trozo_abierto(self):
        t0 = time.time() - 10000
        prot = g.abrir_sesion("Prot", "r", "mp4", t0 - 9000)
        g.nuevo_trozo(prot, t0 - 9000); self.escribir(prot, "trozo-01.mp4", 1, mtime=t0 - 9000)
        g.cerrar_sesion(prot, t0 - 8000); g.proteger(prot, True)
        vieja = g.abrir_sesion("Vieja", "r", "mp4", t0 - 7000)
        g.nuevo_trozo(vieja, t0 - 7000); self.escribir(vieja, "trozo-01.mp4", 1, mtime=t0 - 7000)
        g.nuevo_trozo(vieja, t0 - 6400); self.escribir(vieja, "trozo-02.mp4", 1, mtime=t0 - 6400)
        g.cerrar_sesion(vieja, t0 - 5800)
        self.escribir(None, "suelto.mkv", 1, mtime=t0 - 6000)
        viva = g.abrir_sesion("Viva", "r", "mkv", t0)
        g.nuevo_trozo(viva, t0); self.escribir(viva, "trozo-01.mkv", 1, mtime=t0)
        g.nuevo_trozo(viva, t0 + 600); self.escribir(viva, "trozo-02.mkv", 1, mtime=t0 + 600)
        # el disco "se llena" hasta que se hayan borrado 3 ficheros
        libres = iter([1, 1, 1, 10 ** 12, 10 ** 12, 10 ** 12, 10 ** 12])
        with mock.patch.object(g.shutil, "disk_usage", side_effect=lambda _: types.SimpleNamespace(free=next(libres))):
            borrados = g.liberar_disco(reserva=100, vivas=[viva])
        self.assertEqual([os.path.basename(b) for b in borrados], ["trozo-01.mp4", "trozo-02.mp4", "suelto.mkv"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, vieja)))      # sin trozos: fuera la carpeta
        self.assertTrue(os.path.exists(os.path.join(self.dir, prot, "trozo-01.mp4")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, viva, "trozo-02.mkv")))
        # si hiciera falta más, caería el primer trozo de la viva pero nunca el abierto
        libres = iter([1, 1, 1, 1, 1, 1])
        with mock.patch.object(g.shutil, "disk_usage", side_effect=lambda _: types.SimpleNamespace(free=next(libres))):
            borrados = g.liberar_disco(reserva=100, vivas=[viva])
        self.assertEqual([os.path.basename(b) for b in borrados], ["trozo-01.mkv"])
        self.assertTrue(os.path.exists(os.path.join(self.dir, viva, "trozo-02.mkv")))
        self.assertEqual(len(g._leer(viva)["trozos"]), 1)

    def test_fuente_delega(self):
        import fuente
        with mock.patch.object(g, "liberar_disco", return_value=["x"]) as l:
            self.assertEqual(fuente.liberar_disco(self.dir, kbps=8000), ["x"])
            l.assert_called_once_with(None, 8000)
        otra = tempfile.mkdtemp()
        try:
            with open(os.path.join(otra, "a.mp4"), "wb") as f:
                f.write(b"1")
            with mock.patch.object(fuente.shutil, "disk_usage", return_value=types.SimpleNamespace(free=0)):
                fuente.liberar_disco(otra, reserva=1)
            self.assertEqual(os.listdir(otra), [])
        finally:
            shutil.rmtree(otra, ignore_errors=True)


LSBLK = {"blockdevices": [
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "tran": "nvme", "rm": False, "size": 100, "fstype": None, "label": None, "mountpoint": None, "type": "disk",
     "children": [{"name": "nvme0n1p2", "path": "/dev/nvme0n1p2", "tran": None, "rm": False, "size": 90, "fstype": "ext4", "label": None, "mountpoint": "/", "type": "part"}]},
    {"name": "sdb", "path": "/dev/sdb", "tran": "usb", "rm": True, "size": 64000000000, "fstype": None, "label": None, "mountpoint": None, "type": "disk",
     "children": [{"name": "sdb1", "path": "/dev/sdb1", "tran": None, "rm": True, "size": 63999000000, "fstype": "exfat", "label": "PINCHO", "mountpoint": None, "type": "part"}]},
    {"name": "sdc", "path": "/dev/sdc", "tran": "usb", "rm": False, "size": 10, "fstype": None, "label": None, "mountpoint": None, "type": "disk",
     "children": [{"name": "sdc1", "path": "/dev/sdc1", "tran": None, "rm": False, "size": 9, "fstype": None, "label": None, "mountpoint": None, "type": "part"}]},
]}


class Usb(Base):
    def test_discos(self):
        d = g.usb_discos(LSBLK)
        self.assertEqual(d, [{"dev": "/dev/sdb1", "etiqueta": "PINCHO", "tamano": 63999000000, "fs": "exfat", "montado": None, "libre": None}])

    def test_copiar(self):
        t0 = time.time() - 1000
        s = g.abrir_sesion("Culto", "r1", "mp4", t0)
        g.nuevo_trozo(s, t0); self.escribir(s, "trozo-01.mp4", 3)
        g.nuevo_trozo(s, t0 + 600); self.escribir(s, "trozo-02.mp4", 2)
        g.cerrar_sesion(s, t0 + 900)
        g.marca(s, "Sermón", 100)
        montaje = tempfile.mkdtemp()
        try:
            with mock.patch.object(g, "usb_montar", return_value=montaje):
                self.assertTrue(g.copiar_a_usb("/dev/sdb1", [s, "../no", "no-existe"]))
                g._copia_hilo.join(10)
            e = g.estado_copia()
            self.assertEqual((e["en_curso"], e["hechas"], e["error"]), (False, 1, None))
            self.assertEqual(e["bytes"], e["total_bytes"])
            carpeta = os.path.join(montaje, "NDIICAM", s)
            self.assertEqual(sorted(os.listdir(carpeta)), ["marcas.txt", "sesion.json", "trozo-01.mp4", "trozo-02.mp4"])
            self.assertEqual(os.path.getsize(os.path.join(carpeta, "trozo-01.mp4")), 3 * 1024)
            self.assertIn("00:01:40 Sermón", open(os.path.join(carpeta, "marcas.txt")).read())
            # con unidas, la completa en vez de los trozos (si existe)
            self.escribir(s, "completa.mp4", 5)
            with mock.patch.object(g, "usb_montar", return_value=montaje):
                g.copiar_a_usb("/dev/sdb1", [s], unidas=True)
                g._copia_hilo.join(10)
            self.assertIn("completa.mp4", os.listdir(carpeta))
        finally:
            shutil.rmtree(montaje, ignore_errors=True)

    def test_sin_sesiones(self):
        with self.assertRaises(ValueError):
            g.copiar_a_usb("/dev/sdb1", ["no"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "sin ffmpeg")
class UnirConFfmpeg(unittest.TestCase):
    """Los trozos de una sesión, unidos de verdad: cada uno empieza en 0 (como los
    que deja splitmuxsink) y la completa debe durar la suma, con sus dos pistas."""

    def test_une_trozos_que_empiezan_en_cero(self):
        import subprocess
        import unir
        d = tempfile.mkdtemp()
        try:
            for n in (1, 2, 3):
                subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                                "testsrc=size=160x90:rate=24:duration=2", "-f", "lavfi", "-i",
                                "sine=frequency=440:duration=2", "-c:v", "libx264", "-c:a", "aac",
                                "-shortest", os.path.join(d, f"trozo-0{n}.mp4")], check=True)
            self.assertTrue(unir.unir(d, "mp4"))
            completa = os.path.join(d, "completa.mp4")
            self.assertFalse(os.path.exists(completa + ".tmp"))
            r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                                "-of", "json", completa], capture_output=True, text=True, check=True)
            info = json.loads(r.stdout)
            self.assertAlmostEqual(float(info["format"]["duration"]), 6.0, delta=0.3)
            self.assertEqual(sorted(x["codec_type"] for x in info["streams"]), ["audio", "video"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_sin_trozos_no_deja_nada(self):
        import unir
        d = tempfile.mkdtemp()
        try:
            self.assertFalse(unir.unir(d, "mp4"))
            self.assertEqual(os.listdir(d), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
