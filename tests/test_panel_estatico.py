# NDIICAM — comprobaciones estáticas del panel web (sin navegador).
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""Lo que se puede comprobar del panel leyendo el HTML:

- cada código de error que devuelve panel.py tiene traducción en los dos idiomas
  (si no, el usuario ve el código a pelo);
- los diccionarios no repiten claves (una repetida gana a la otra en silencio:
  así todos los errores de configuración decían "Resolución no válida");
- el generador de capturas (docs/screenshots/demo.py) produce todas las vistas.
"""
import importlib.util
import os
import re
import sys
import tempfile
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(RAIZ, "ndiicam", "web", "index.html")
PANEL = os.path.join(RAIZ, "ndiicam", "panel.py")
DEMO = os.path.join(RAIZ, "docs", "screenshots", "demo.py")

# códigos que el panel traduce de otra forma o que no llegan al usuario como texto
EXCEPCIONES = set()


def leer(ruta):
    with open(ruta, encoding="utf-8") as f:
        return f.read()


def sin_cadenas(js):
    """El JS sin literales de texto ni plantillas: así los dos puntos de una URL
    dentro de un mensaje no parecen claves."""
    js = re.sub(r"`(?:\\.|[^`\\])*`", "``", js, flags=re.S)
    js = re.sub(r'"(?:\\.|[^"\\])*"', '""', js)
    js = re.sub(r"'(?:\\.|[^'\\])*'", "''", js)
    return js


def bloque(js, inicio):
    """El texto entre la llave que abre tras `inicio` (una expresión regular) y la que la cierra."""
    m = re.search(inicio, js)
    if not m:
        raise ValueError(inicio)
    i = js.index("{", m.start())
    n, j = 0, i
    while j < len(js):
        if js[j] == "{":
            n += 1
        elif js[j] == "}":
            n -= 1
            if n == 0:
                return js[i:j + 1]
        j += 1
    raise ValueError(inicio)


def claves(objeto_js):
    """Las claves de un objeto JS plano (ya sin cadenas): lo que va antes de ':'
    y viene tras '{' o ','."""
    return re.findall(r"(?<=[{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", objeto_js)


def claves_por_objeto(objeto_js):
    """Recorre un objeto JS (sin cadenas) con sus subobjetos y devuelve, por
    cada objeto abierto, la lista de sus claves con el nombre del padre."""
    pila, resultado = [], []
    for m in re.finditer(r"[{}]|(?<=[{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", objeto_js):
        if m.group(0) == "{":
            nombre = pila[-1][1][-1] if pila and pila[-1][1] else ""
            pila.append((nombre, []))
        elif m.group(0) == "}":
            resultado.append(pila.pop())
        elif pila:
            pila[-1][1].append(m.group(1))
    return resultado


def diccionario(js, idioma, nombre):
    """El objeto de un idioma dentro de T o RT."""
    raiz = bloque(js, rf"const {nombre} = ")
    return bloque(raiz, rf"\n  {idioma}: ")


class Traducciones(unittest.TestCase):
    def setUp(self):
        self.js = sin_cadenas(leer(WEB))
        self.codigos = set(re.findall(r'_error\("([a-z_]+)"', leer(PANEL))) - EXCEPCIONES

    def traducidos(self, idioma):
        t = diccionario(self.js, idioma, "T")
        rt = diccionario(self.js, idioma, "RT")
        return set(claves(bloque(t, r"(?<![A-Za-z_])errors:"))) | set(claves(bloque(rt, r"(?<![A-Za-z_])errores:")))

    def test_todo_codigo_del_servidor_tiene_texto(self):
        self.assertTrue(self.codigos, "no se encontraron códigos en panel.py")
        for idioma in ("es", "en"):
            faltan = sorted(self.codigos - self.traducidos(idioma))
            self.assertEqual(faltan, [], f"códigos sin traducir en {idioma}: {faltan}")

    def test_sin_claves_repetidas(self):
        for nombre in ("T", "RT"):
            for idioma in ("es", "en"):
                repetidas = []
                for padre, ks in claves_por_objeto(diccionario(self.js, idioma, nombre)):
                    vistos = set()
                    for k in ks:
                        if k in vistos:
                            repetidas.append(f"{padre}.{k}" if padre else k)
                        vistos.add(k)
                self.assertEqual(repetidas, [], f"claves repetidas en {nombre}.{idioma}: {repetidas}")

    def test_los_dos_idiomas_tienen_las_mismas_claves(self):
        for nombre in ("T", "RT"):
            es = {(p, k) for p, ks in claves_por_objeto(diccionario(self.js, "es", nombre)) for k in ks}
            en = {(p, k) for p, ks in claves_por_objeto(diccionario(self.js, "en", nombre)) for k in ks}
            self.assertEqual(sorted(es ^ en), [], f"claves que solo están en un idioma de {nombre}")


class GeneradorDeCapturas(unittest.TestCase):
    def test_genera_todas_las_vistas(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("sin Pillow")
        spec = importlib.util.spec_from_file_location("demo", DEMO)
        demo = importlib.util.module_from_spec(spec)
        sys.path.insert(0, os.path.dirname(DEMO))
        spec.loader.exec_module(demo)
        vistas = re.findall(r"^\s{4}([a-z-]+)\s{2,}", demo.__doc__, flags=re.M)
        self.assertGreaterEqual(len(vistas), 6)
        with tempfile.TemporaryDirectory() as d:
            for vista in vistas:
                for lang in ("es", "en"):
                    salida = os.path.join(d, f"{vista}-{lang}.html")
                    demo.estatico(salida, lang, "claro", vista)
                    html = leer(salida)
                    self.assertIn("DEMO_API", html, vista)
                    self.assertIn("refrescarT = setTimeout(refrescar, 30)", html, "la demo no engancha el temporizador")


if __name__ == "__main__":
    unittest.main()
