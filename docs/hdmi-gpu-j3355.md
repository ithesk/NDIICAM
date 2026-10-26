# HDMI por GPU en el Intel J3355 / HD Graphics 500

Validado el 26 de octubre de 2026 en un mini-PC con Celeron J3355, Ubuntu
24.04.5, kernel 6.8.0-147, GStreamer 1.24.2, driver VA iHD 24.1.0 y HuC activo.
Cámara: DJI Pocket 3, MJPEG baseline 4:2:0, 1920×1080 a 30 fps, USB 2.0.
Monitor: 2560×1440; el plano de pantalla escala el vídeo 1080p.

## Cambio aplicado

Antes:

```text
Cámara MJPEG -> jpegdec (CPU) -> videoconvert UYVY (CPU) -> kmssink -> HDMI
```

Ahora, cuando la ruta solo necesita HDMI:

```text
Cámara MJPEG -> appsink/appsrc (solo JPEG comprimido)
            -> vajpegdec -> vapostproc -> YUYV DMA-BUF con modificador Intel Y-tiled
            -> ndihdmisink -> plano DRM -> HDMI
```

El submuestreo se lee del primer JPEG real. Los fotogramas sin comprimir no
se mapean ni se descargan a memoria de CPU. Las colas tienen tamaño limitado
y descartan fotogramas antiguos si hay retraso. Los pipelines comparten reloj.
La vista previa sigue copiando el JPEG comprimido cada dos segundos.
La conversión VA negocia BT.709 para la salida.

La comprobación DRM mostró `YUYV`, modificador `0x0100000000000002`, paso de
3840 bytes y tamaño 1920×1080. Los planos de esta GPU no anunciaban NV12;
por eso no basta conectar el decodificador NV12 directamente a la pantalla.

El `kmssink` de Ubuntu 24.04 no anuncia `DMA_DRM`. Se añadió un sumidero
privado `ndihdmisink`, derivado del código oficial GStreamer 1.26.10 y compatible
con las bibliotecas 1.24 del servidor. No se sustituyó el GStreamer del sistema.
Fuentes, licencia y compilación: [plugin/hdmi/README.md](../plugin/hdmi/README.md).

## Cuándo se activa y cómo recupera

`fuente.hdmi_gpu_disponible()` exige MJPEG, HDMI, ausencia de salida NDI y de
audio en ese proceso, los elementos VA y el módulo instalado junto a la app.
El archivo local se carga explícitamente; no se usa una copia de una caché
que apunte a directorios de pruebas.

- Sin módulo compatible: se conserva la ruta CPU anterior.
- Si el proceso GPU informa un error, muere o deja de dar imagen: el supervisor
  reintenta con CPU, conservando la asignación HDMI.
- El veto de GPU para esta fuente dura hasta reiniciar el servicio o usar el
  botón Reiniciar del panel. No se reintenta la GPU sin límite.
- Al añadir NDI o audio, se vuelve a la ruta existente. No se cambiaron rutas,
  resolución, configuración de red ni preferencias del usuario.

La lista `GPU_SIN_VPP` de `gpu.py` sigue protegiendo la ruta que descarga UYVY
para NDI: esa ruta fue más lenta. El indicador global `gpu: false` de la
clasificación Lite/Pro evalúa esa capacidad; no significa que el nuevo HDMI
no use GPU. Para HDMI existen los eventos `hdmi_gpu` y `hdmi_gpu_cpu`.

## Mediciones

Comparación en el HDMI físico con un JPEG real de la cámara, repetido durante
aproximadamente cuatro segundos:

| Ruta | Fotogramas que llegaron al sumidero | Tiempo de CPU | Tiempo real |
|---|---:|---:|---:|
| CPU anterior | 100 de 120 | 2,7217 s | 4,0691 s |
| GPU DMA-BUF | 120 de 120 | 0,6506 s | 4,1144 s |

La prueba anterior puede descartar por QoS; no se presenta como comparación
de idéntico número de fotogramas terminados. Sirvió para verificar negociación,
importación DMA-BUF y salida física antes de tocar la aplicación.

Medición del proceso `fuente.py` con la **cámara real en funcionamiento**:

| Estado | CPU del proceso, respecto a un núcleo |
|---|---:|
| Antes, muestra de 10 s | 76,2 % |
| Después, muestra de 10 s | 20,1 % |
| Segunda muestra después de estabilizar, 10 s | 19,7 % |

Reducción observada de aproximadamente **74 %** en ese proceso. No equivale
a 74 puntos de CPU del sistema completo ni garantiza el mismo resultado con
otra imagen, cámara, formato o resolución. La carga depende del contenido.

No se observaron reinicios del servicio ni mensajes de GPU hang/reset/fault
durante la validación. Esto no sustituye una prueba de funcionamiento de horas.
La confirmación visual de colores y encuadre por el operador sigue siendo útil.

Validación final del proceso desplegado: cuatro informes durante 6,01 s,
**29,95 fps de cámara y 29,95 fps de HDMI**, cero descartes de cola y **10 ms**
de latencia interna de captura a entrada del sumidero (no latencia total del
monitor). Se confirmó en `/proc/PID/maps` que carga
`/opt/ndiicam/gst/libgstndihdmikms.so`. Evidencia en `telemetria-hdmi.json` y
`telemetria-hdmi.log`, sin consumir el canal IPC del supervisor.

## Código, pruebas e instalación

- `ndiicam/fuente.py`: `HdmiGPU`, selección automática, caps del primer JPEG,
  etiquetado de errores y contadores.
- `ndiicam/supervisor.py`: recuperación CPU sin quitar la pantalla.
- `plugin/hdmi/`: sumidero privado, fuentes LGPL y script de construcción.
- `install.sh`: intenta compilar el módulo; conserva CPU si no puede.
- `tests/test_ndiicam.py`: exclusión de NDI/audio, módulo local obligatorio y
  recuperación ante error, muerte o falta de imagen.

En Linux, desde el repositorio:

```sh
sudo apt-get install build-essential pkg-config libgstreamer-plugins-base1.0-dev libdrm-dev
sudo sh plugin/hdmi/build.sh /opt/ndiicam/gst/libgstndihdmikms.so
sudo install -m 755 ndiicam/fuente.py ndiicam/supervisor.py /opt/ndiicam/
sudo systemctl restart ndiicam
journalctl -u ndiicam --since '2 minutes ago' | grep hdmi_gpu
```

Las pruebas de `test_ndiicam.py` pasan.

## Cómo volver a la ruta por CPU

Basta con quitar el módulo y reiniciar el servicio: `fuente.py` no lo encuentra
y usa la ruta anterior. No toca la configuración.

```sh
sudo rm /opt/ndiicam/gst/libgstndihdmikms.so
sudo systemctl restart ndiicam
```

## Referencias

- [GStreamer KMS upstream 1.26.10](https://github.com/GStreamer/gstreamer/tree/1.26.10/subprojects/gst-plugins-bad/sys/kms)
- [Decodificador VA JPEG](https://gstreamer.freedesktop.org/documentation/va/vajpegdec.html)
- [Procesador de vídeo VA](https://gstreamer.freedesktop.org/documentation/va/vapostproc.html)
- [Salida KMS](https://gstreamer.freedesktop.org/documentation/kms/index.html)
