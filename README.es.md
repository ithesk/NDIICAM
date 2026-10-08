# NDIICAM

**Convierte cualquier PC x86_64 con Linux (Intel/AMD) y una cámara USB en una cámara NDI® lista para usar.**

[English](README.md)

Conectas una cámara USB a un PC pequeño y aparece como fuente NDI en tu red: en OBS,
vMix, NDI Studio Monitor o cualquier receptor NDI. Todo se gestiona desde un panel web:
tras la instalación no hace falta la terminal.

- **Emite cada cámara UVC conectada como fuente NDI propia** (MJPEG o YUYV) a 720p o 1080p,
  hasta 60 fps, **con el micrófono de la cámara** como audio NDI si lo tiene.
- **Panel web** en `http://<equipo>.local`: estado en vivo, fps, tasa de datos, receptores
  conectados, señal WiFi y uso del enlace, CPU y temperatura. En español e inglés.
- **WiFi de configuración**: si no hay ninguna red conocida, el equipo crea su propia WiFi
  con portal cautivo. Te conectas desde el móvil, eliges tu red, pones la clave y listo.
- **Se recupera solo**: detecta la cámara al enchufarla, reinicia la emisión si la cámara
  deja de dar imagen y vuelve a las redes conocidas cuando reaparecen.
- **Monitor de encuadre por HDMI**: muestra una cámara a pantalla completa en el monitor conectado al
  equipo, directo a la GPU y sin gastar CPU. Se activa y desactiva desde el panel.
- **IP fija, copia de seguridad, valores de fábrica y actualización con un clic** desde el panel.
- **Ligero**: un único servicio en Python sobre GStreamer, sin frameworks web ni base de datos.

## Requisitos

| | |
|---|---|
| CPU | x86_64 Intel o AMD. Un Atom x5-Z8350 (4 núcleos, 1,44 GHz) mueve 1080p30 con ~1,6 núcleos |
| Sistema | Ubuntu 24.04 LTS (probado). Debian 12+ debería funcionar |
| Cámara | Cámara USB UVC con MJPEG (la mayoría de webcams, DJI Osmo Pocket 3 en modo webcam…) |
| Red | Mejor por cable. Por WiFi funciona: 1080p30 son ~90 Mbit/s por receptor |
| Tarjeta WiFi (opcional) | Necesaria para la WiFi de configuración. Las que admiten AP y cliente a la vez (p. ej. Intel 3165) no sueltan la conexión mientras está activa |

## Instalación

```bash
git clone https://github.com/ithesk/NDIICAM.git
cd NDIICAM
sudo ./install.sh
```

O en una línea:

```bash
curl -fsSL https://raw.githubusercontent.com/ithesk/NDIICAM/main/install.sh | sudo bash -s -- --accept-ndi-license
```

El instalador:

1. Instala las dependencias (GStreamer, NetworkManager, hostapd, Avahi…).
2. Descarga el runtime del **NDI® SDK** de los servidores oficiales de NDI. Es software
   privativo de Vizrt NDI AB: te pide aceptar su licencia (`--accept-ndi-license` la
   acepta sin preguntar).
3. Instala el plugin NDI de GStreamer de
   [gst-plugins-rs](https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs) (MPL-2.0),
   precompilado por la CI de este repositorio (`--build-plugin` lo compila en el equipo).
4. Instala y arranca el servicio `ndiicam`.
5. Pasa la red a **NetworkManager** si la gestionan netplan y systemd-networkd (Ubuntu
   Server). La red se corta unos segundos; si no vuelve en 90 s, se restaura sola la
   configuración anterior. Se omite con `--no-network`. Conserva la identidad DHCP, así que
   el router sigue dando la misma IP.

Volver a ejecutar el instalador actualiza NDIICAM y conserva la configuración.

## Primer uso

1. Conecta la cámara. La emisión empieza sola.
2. Abre `http://<equipo>.local` (el instalador te da la dirección).
3. En OBS: *Añadir fuente → NDI Source* (con [DistroAV](https://github.com/DistroAV/DistroAV))
   y elige `<EQUIPO> (NDIICAM)`.

**¿Sin red?** Tras 60 s sin ninguna red conocida, el equipo activa la WiFi de configuración
`NDIICAM-XXXX` (clave `ndiicam-setup`, cámbiala en el panel). Conéctate desde el móvil: el
panel se abre solo. Elige tu red y escribe su clave. El resultado sale en la misma pantalla;
después vuelve a tu red y abre `http://<equipo>.local`.

## Panel

| Sección | Qué hace |
|---|---|
| Estado | Estado general, fuentes emitiendo, receptores, Mbit/s saliendo, CPU, gráficas de los últimos 2 minutos |
| Una tarjeta por cámara | Activar/desactivar, nombre NDI, modo, fps de cámara/enviados, tasa, descartes, audio, receptores por nombre; ajustes: nombre NDI, 720p/1080p, fps, audio (micro de la cámara, otra entrada o ninguno) |
| Salida HDMI | Activar/desactivar, qué cámara, estado del monitor, resolución de la pantalla al arrancar |
| Red | Señal WiFi, uso del enlace frente a la capacidad estimada, banda, canal, IP, transporte NDI |
| Actualizaciones | Versión instalada y última, comprobar, instalar con un clic |
| Redes WiFi | Redes guardadas (olvidar), buscar, conectar |
| Dirección IP | DHCP o fija. Si con la fija no hay conexión, vuelve sola a la anterior |
| Equipo | Nombre del equipo, transporte NDI, nombre y clave de la WiFi de configuración, país, contraseña opcional del panel, descargar/restaurar copia, valores de fábrica, reiniciar |

La contraseña del panel no se pide desde la WiFi de configuración: quien está en ella ya
conoce su clave, y es la forma de volver a entrar si se olvida la del panel.

## Notas técnicas

Salen de montarlo en hardware real y explican algunos valores por defecto:

- **El transporte NDI va por TCP.** Con el UDP fiable que NDI 6 usa por defecto, algunos
  receptores (OBS en macOS en nuestras pruebas) veían la fuente pero nunca recibían vídeo.
  TCP funciona en todos. Se puede cambiar a UDP en el panel.
- **Avahi es imprescindible.** El NDI SDK en Linux anuncia las fuentes a través de Avahi;
  sin él la emisión funciona pero ningún receptor la encuentra.
- **Se desactiva el ahorro de energía de la WiFi** (`wifi.powersave = 2`): provoca tirones
  en un flujo constante de ~90 Mbit/s.
- **El pipeline va repartido en hilos.** Decodificación, conversión de color y codificación
  NDI corren en hilos distintos con colas que descartan. En un solo hilo, un Atom solo llega
  a ~22 fps a 1080p; repartido, mantiene 30 fps. Si la CPU se queda atrás, se descartan
  fotogramas viejos en vez de acumular retraso.
- **Sin VAAPI.** En Intel Cherry Trail la iGPU decodifica JPEG, pero los fotogramas
  decodificados no se pueden bajar a memoria (fallan todos), y la codificación SpeedHQ de
  NDI es solo por CPU. NDI|HX con H.264 por hardware exigiría el NDI Advanced SDK.
- **La salida HDMI va directa a un plano de vídeo de la GPU** (`kmssink`, UYVY): no gasta CPU.
  Algunos planos no escalan (Intel Cherry Trail): para verla a pantalla completa, pon en el panel la
  resolución de la pantalla igual a la de la cámara. Escribe `video=` en
  `/etc/default/grub.d/90-ndiicam.cfg` y requiere reiniciar. Las entradas de GRUB personalizadas con
  la línea de arranque fija no lo recogen: añádeles `video=1920x1080@60` a mano. Convertir a RGB para
  escalar por CPU cuesta +70-150% de CPU en un Atom.
- **El consumo del procesador** del panel sale de Intel RAPL y solo cubre el SoC (CPU + GPU), no la
  WiFi, la memoria, la placa ni la cámara. En el Atom: 0,9 W en reposo, 1,7 W emitiendo 1080p30.
- **La WiFi de configuración usa una interfaz virtual `ap0`** cuando la tarjeta lo permite,
  así el equipo sigue buscando sus redes mientras tanto. Si no, alterna: 3 minutos de AP y
  45 s buscando redes, mientras nadie esté conectado al AP.

## Problemas frecuentes

| Problema | Qué mirar |
|---|---|
| La fuente no aparece en el receptor | `avahi-daemon` activo; receptor y equipo en la misma red/VLAN; que la WiFi no aísle a los clientes ni bloquee mDNS |
| Aparece la fuente pero sin imagen | Pon el transporte en TCP desde el panel |
| Tirones por WiFi | Barra de uso del enlace en el panel; 5 GHz; 720p; mejor por cable |
| El panel no abre | `systemctl status ndiicam`; otro programa en el puerto 80 |
| Registros | `journalctl -u ndiicam -f`; el del cambio de red en `/var/lib/ndiicam/red.log` |

## Desinstalar

```bash
sudo ./uninstall.sh                  # quita NDIICAM y deja la red como está
sudo ./uninstall.sh --restore-network --purge   # además restaura netplan/networkd y borra SDK, plugin y ajustes
```

## Licencia

NDIICAM es software libre bajo la **GNU General Public License v3.0 o posterior**
([LICENSE](LICENSE)). Copyright © 2026 Pablo Holguín.

NDIICAM no incluye el NDI® SDK: el instalador lo descarga de NDI y se rige por su propia
licencia. **NDI® es una marca registrada de Vizrt NDI AB.** NDIICAM no está afiliado a
Vizrt NDI AB ni cuenta con su respaldo. El plugin NDI de GStreamer forma parte de
gst-plugins-rs (MPL-2.0).
