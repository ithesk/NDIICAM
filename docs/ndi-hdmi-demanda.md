# NDI y HDMI simultáneos, con NDI bajo demanda

Probado en un Intel Celeron J3355 / HD Graphics 500 con una DJI Pocket 3
a 1080p30 MJPEG, en una ruta con salida HDMI y NDI.

## Funcionamiento

```text
Cámara USB MJPEG (una sola captura)
  ├─ JPEG -> decodificador VA -> VPP -> DMA-BUF -> HDMI
  └─ JPEG -> cola independiente -> jpegdec I420 -> NDI
                                  solo con receptores conectados
```

La imagen HDMI permanece en la GPU como en la optimización anterior.
La rama NDI recibe referencias a los JPEG comprimidos; no descarga la imagen
del framebuffer HDMI ni bloquea esperando al decodificador o al envío de red.
Sus colas tienen límites y descartan imágenes antiguas ante congestión.

El emisor NDI se mantiene anunciado aunque no tenga receptores. La propiedad
`connections` del plugin parcheado se consulta cada 250 ms. Con cero receptores
no se pasan JPEG al decodificador NDI; al conectar uno, se reanuda la rama sin
reiniciar el proceso, cambiar el reloj ni volver a abrir la cámara/HDMI.
Con varios receptores se usa el mismo emisor del SDK, no un pipeline por cliente.

Para NDI se deja el I420 nativo de `jpegdec`, evitando la conversión obligatoria
a UYVY. El formato H.264 no se emplea como sustituto de NDI High Bandwidth.
La compresión de este NDI sigue ejecutándose por software: el aprovechamiento
de GPU corresponde al HDMI. No se promete una gran reducción de CPU cuando
NDI está transmitiendo continuamente.

## Condiciones y recuperación

- MJPEG con HDMI asignado y módulo `ndihdmisink` instalado.
- Para usar también NDI, el `ndisink` instalado debe exponer `connections`.
  Sin ese parche se conserva la ruta anterior que siempre procesa vídeo.
- Esta ruta se activa sin audio; las configuraciones con audio conservan la
  ruta existente. Se validó vídeo a 1080p30, no 4K ni audio simultáneo.
- `NdiDemanda` tiene su propio pipeline, bus de errores y recuperación.
  Un error NDI apaga y reintenta esa rama después de tres segundos, sin
  desactivar HDMI ni marcar la GPU como fallida.
- Un error del pipeline HDMI/GPU conserva el mecanismo anterior de recuperación
  completa por CPU. [Detalles de HDMI](hdmi-gpu-j3355.md).

## Pruebas con la aplicación real

Se conectó y desconectó un receptor NDI local dos veces. El proceso de cámara
fue el mismo durante todas las fases, sin reinicios.

| Fase | CPU del proceso respecto a un núcleo | HDMI | NDI |
|---|---:|---:|---:|
| Sin receptor NDI | ~19–21 % | ~30 fps | 0 fps, anunciado |
| Un receptor conectado | ~93 % | ~30 fps | ~30 fps |
| Tras desconectar | ~20 % | ~30 fps | 0 fps, anunciado |

Primera ejecución: 20,11 %, 92,66 % y 19,98 % respectivamente. La segunda
ejecución reprodujo el comportamiento: 19,24 %, 92,63 % y 19,86 %.
El primer receptor recibió 296 fotogramas durante aproximadamente diez segundos
de conexión. El periodo estable medido registró 29,97 fps en ambas salidas.
La latencia interna de captura hasta el sumidero HDMI se mantuvo alrededor
de 10 ms. Esta no es la latencia total de cámara a monitor.

El receptor de prueba estaba en el mismo equipo. Su consumo no está incluido
en la CPU del proceso emisor, pero compite por los dos núcleos. Son mediciones
cortas de vídeo real: no son una garantía para horas de carga ni otros modos.
No se observaron GPU hangs, resets o reinicios del servicio en la validación.

## Comparación del formato NDI

Emisión de 120 JPEG reales repetidos a 1080p30, con receptor local en un proceso
separado, manteniendo activa la aplicación HDMI. Los tiempos incluyen decodificar
y enviar NDI; se excluyen la generación de la muestra y el arranque del receptor.

| Formato | Tiempo de CPU del emisor de prueba | Fotogramas recibidos |
|---|---:|---:|
| I420 | 3,2499 s | 120/120 |
| NV12 | 3,6218 s | 118/120 |
| UYVY | 3,3807 s | 120/120 |

La diferencia entre I420 y UYVY es pequeña en esta muestra. Se eligió I420 por
ser la salida nativa del decodificador y tener el menor coste observado; no se
atribuye la pérdida puntual de dos fotogramas NV12 exclusivamente al formato.
Todos los receptores terminaron sin errores. La fuente JPEG ya es 4:2:0.

## Código y diagnóstico

- `ndiicam/fuente.py`: `NdiDemanda`, `tramo_ndi_jpeg`, y la rama opcional de `HdmiGPU`.
- `ndiicam/supervisor.py`: registra `ndi_rama_fallida` sin desmontar HDMI.
- `tests/test_ndiicam.py`: ausencia/presencia de clientes, ausencia del parche
  de conexiones, límites de selección, y recuperación de errores NDI aislados.
- Los informes del proceso incluyen `ndi_out`, `ndi_desc`, `ndi_activo`,
  `ndi_formato` y `ndi_error`, además de `hdmi_gpu` y `conexiones`.
  `out` sigue contando el camino HDMI, para que NDI dormido no dispare
  falsamente la recuperación por falta de imagen.

También se inyectó un mensaje de error GStreamer en una instancia de prueba de
`NdiDemanda`: reintentó su pipeline y volvió a PLAYING sin reiniciar el servicio
real.

Pruebas del módulo principal: 102 correctas. Suite completa: 208 pruebas,
un fallo preexistente de MediaMTX y una omitida; no aparecieron nuevos fallos.
El fallo es `test_motor_rutas.ConfMediaMTX.test_una_sola_consulta_para_todos_los_caminos`,
reproducido antes de este trabajo con el código original.

Los registros se tomaron con `strace` sin consumir el canal IPC del supervisor.
La captura de telemetría añade algo de sobrecarga; no se presenta como un perfil
preciso de rendimiento absoluto.

## Instalación y restauración

Se reutiliza el módulo HDMI ya instalado en `/opt/ndiicam/gst/`. Solo se
actualizaron `fuente.py` y `supervisor.py`, sin cambios de configuración.
La compilación del sumidero está descrita en
[plugin/hdmi](../plugin/hdmi/README.md) y la incluye `install.sh`.

Para volver a la ruta anterior (NDI siempre decodificado por CPU) basta con
un `ndisink` sin la propiedad `connections`, o con quitar el HDMI o añadir
audio a la ruta: `hdmi_gpu_disponible()` deja entonces el camino convencional.
