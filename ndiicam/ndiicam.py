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
- Sirve el panel web por HTTPS (443; el 80 redirige): estado, ajustes, redes
  WiFi, IP fija, copias de seguridad, actualización y registro. Con contraseña.
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
  seguridad     sesiones del panel, límite de intentos, certificado HTTPS
  cortafuegos   nftables: solo entra lo que el modo activo necesita
  sistema       watchdog, motivo del último reinicio, registro, zona horaria
"""
import glob
import os
import signal
import socket
import threading
import time

import nucleo
import sistema
from actualizador import Actualizador
from cortafuegos import Cortafuegos
from estado import medidor
from nucleo import DATOS, VERSION, Config, bucles_vivos, cargar_eventos, configurar_ndi, evento
from panel import Panel, Servidor, ServidorTLS
from red import Red
from seguridad import CERT, LimiteIntentos, Sesiones, contexto_tls
from supervisor import BuscadorNDI, Fuentes

# el watchdog de systemd (WatchdogSec en el unit): este hilo le dice "sigo vivo"
# cada LATIDO s, pero solo si todos los bucles del servicio han dado una vuelta
# en los últimos BUCLE_MAX s. Un bloqueo de verdad (un lock que no se suelta) se
# nota a los WatchdogSec; una vuelta lenta (cambiar la IP y esperar a que
# vuelva la red tarda hasta 90 s) no
LATIDO = 20
BUCLE_MAX = 150


# el socket de systemd es solo del proceso principal: se saca del entorno para que
# no lo hereden los hijos (gst, nmcli, ss...), que si no lo usan por su cuenta y
# systemd los rechaza con un aviso en el diario cada pocos segundos
NOTIFY_SOCKET = os.environ.pop("NOTIFY_SOCKET", None)


def sd_notify(mensaje):
    """Mensaje a systemd por su socket, sin dependencias. Sin systemd, nada."""
    ruta = NOTIFY_SOCKET
    if not ruta:
        return
    if ruta.startswith("@"):
        ruta = "\0" + ruta[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as so:
            so.connect(ruta)
            so.sendall(mensaje.encode())
    except OSError:
        pass


def latir():
    while True:
        if bucles_vivos(BUCLE_MAX):
            sd_notify("WATCHDOG=1")
        else:
            evento("bucle_parado", detalle=",".join(
                n for n, t in nucleo.LATIDOS.items() if time.monotonic() - t >= BUCLE_MAX))
        time.sleep(LATIDO)


def esperar_reloj(maximo=120):
    """La primera vez, el certificado se fecha con el reloj del equipo: si la
    placa arrancó en 2015 y luego NTP lo pone en hora, el certificado "aún no
    vale" y los navegadores lo rechazan. Esperar un poco a NTP antes de crearlo."""
    if os.path.exists(CERT):
        return
    fin = time.monotonic() + maximo
    while time.monotonic() < fin:
        h = sistema.hora()
        if h["sincronizada"] or not h["ntp"]:
            return
        sistema.hora.olvidar()
        time.sleep(5)


def main():
    os.makedirs(DATOS, exist_ok=True)
    # el driver moderno de Intel para todo (hijos y el buscador NDI): si unos
    # procesos lo llevan y otros no, GStreamer rehace su registro de plugins en
    # cada arranque de proceso (el plugin va cambia con esa variable)
    if glob.glob("/usr/lib/*/dri/iHD_drv_video.so"):
        os.environ.setdefault("LIBVA_DRIVER_NAME", "iHD")
    cargar_eventos()
    nucleo.CONF = Config()
    configurar_ndi(nucleo.CONF.get()["transporte"])
    nucleo.FUENTES = Fuentes(nucleo.CONF)
    nucleo.BUSCADOR = BuscadorNDI()
    nucleo.RED = Red(nucleo.CONF)
    nucleo.ACTUALIZADOR = Actualizador()
    nucleo.CORTAFUEGOS = Cortafuegos()
    nucleo.SESIONES = Sesiones()
    nucleo.INTENTOS = LimiteIntentos()
    esperar_reloj()
    nucleo.HTTPS = contexto_tls()
    evento("inicio", version=VERSION)
    sistema.MOTIVO = sistema.calcular_motivo()
    for tarea in (nucleo.FUENTES.bucle, nucleo.RED.bucle, medidor, nucleo.ACTUALIZADOR.bucle,
                  nucleo.CORTAFUEGOS.bucle):
        threading.Thread(target=tarea, daemon=True).start()
    servidor = Servidor(("::", 80), Panel)
    if nucleo.HTTPS is not None:
        cifrado = ServidorTLS(("::", 443), Panel, nucleo.HTTPS)
        threading.Thread(target=cifrado.serve_forever, daemon=True).start()
    else:
        evento("sin_https")
    sd_notify("READY=1")
    threading.Thread(target=latir, daemon=True).start()

    def salir(*_):
        fin = sistema.apagado_en_curso()
        if fin:
            sistema.marcar_fin(fin, sobrescribir=False)
        nucleo.CORTAFUEGOS.detener()
        nucleo.RED.detener_ap()
        nucleo.FUENTES.detener_todas()
        os._exit(0)
    signal.signal(signal.SIGTERM, salir)
    signal.signal(signal.SIGINT, salir)
    servidor.serve_forever()


if __name__ == "__main__":
    main()
