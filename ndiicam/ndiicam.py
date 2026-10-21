#!/usr/bin/env python3
# NDIICAM — convierte un PC Intel con Linux en una cámara NDI.
# Copyright (C) 2026 Pablo Holguín
#
# Este programa es software libre: puedes redistribuirlo y/o modificarlo bajo
# los términos de la GNU General Public License publicada por la Free Software
# Foundation, versión 3 o (a tu elección) cualquier versión posterior.
# Se distribuye SIN NINGUNA GARANTÍA. Ver el fichero LICENSE.
"""Servicio de NDIICAM: arranca los hilos y el servidor web.

- Emite cada cámara UVC conectada (MJPEG o YUYV) como fuente NDI, con el audio
  de la propia cámara si lo tiene. Cada fuente corre en su proceso (fuente.py).
- Salida HDMI: una de las cámaras, para encuadrar.
- Modo receptor: en vez de emitir, muestra por HDMI una fuente NDI de la red
  con su audio (receptor.py). Las cámaras se paran: no hay CPU para las dos cosas.
- Sirve el panel web en el puerto 80: estado, ajustes, redes WiFi, IP fija,
  copias de seguridad y actualización.
- Si no hay ninguna red conocida, levanta una WiFi de configuración con portal
  cautivo (hostapd + dnsmasq). Si la tarjeta lo permite, en una interfaz
  virtual (ap0) sin soltar la WiFi cliente; si no, alternando con ella.

Los eventos y errores viajan como códigos: el panel los traduce.

Módulos (todos en este directorio):
  nucleo        constantes, utilidades, eventos, Config y los singletons
  camaras       cámaras USB, pantallas HDMI, audio
  gpu           capacidad y uso de la GPU Intel, RAPL, CPU
  puente        MediaMTX y ajustes para el ATEM (modo puente)
  supervisor    procesos de cámara, receptor y pantalla de ayuda; reparto del HDMI
  red           WiFi, NetworkManager, WiFi de configuración, IP fija
  actualizador  actualizaciones, copias, fábrica, nombre del equipo
  estado        lo que ve el panel, medido cada 2 s
  panel         servidor web y API
"""
import os
import signal
import threading

import nucleo
from actualizador import Actualizador
from estado import medidor
from nucleo import DATOS, VERSION, Config, configurar_ndi, evento
from panel import Panel, Servidor
from red import Red
from supervisor import BuscadorNDI, Fuentes


def main():
    os.makedirs(DATOS, exist_ok=True)
    nucleo.CONF = Config()
    configurar_ndi(nucleo.CONF.get()["transporte"])
    nucleo.FUENTES = Fuentes(nucleo.CONF)
    nucleo.BUSCADOR = BuscadorNDI()
    nucleo.RED = Red(nucleo.CONF)
    nucleo.ACTUALIZADOR = Actualizador()
    evento("inicio", version=VERSION)
    for tarea in (nucleo.FUENTES.bucle, nucleo.RED.bucle, medidor, nucleo.ACTUALIZADOR.bucle):
        threading.Thread(target=tarea, daemon=True).start()
    servidor = Servidor(("::", 80), Panel)

    def salir(*_):
        nucleo.RED.detener_ap()
        nucleo.FUENTES.detener_todas()
        os._exit(0)
    signal.signal(signal.SIGTERM, salir)
    signal.signal(signal.SIGINT, salir)
    servidor.serve_forever()


if __name__ == "__main__":
    main()
