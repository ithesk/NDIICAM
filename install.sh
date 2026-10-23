#!/usr/bin/env bash
# NDIICAM — instalador para PCs x86_64 (Intel/AMD) con Ubuntu 24.04 o Debian 12+.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
#
#   sudo ./install.sh [--accept-ndi-license] [--no-network] [--build-plugin]
#
# Se puede volver a ejecutar para actualizar: conserva la configuración.
set -euo pipefail

REPO="${NDIICAM_REPO:-ithesk/NDIICAM}"
RAMA="${NDIICAM_BRANCH:-main}"
# el SDK no tiene una URL por versión ni publica hashes: se descarga por TLS del
# servidor oficial y se acepta su licencia. Lo demás (MediaMTX, el plugin) va con
# versión y SHA-256 fijados
NDI_SDK_URL="https://downloads.ndi.tv/SDK/NDI_SDK_Linux/Install_NDI_SDK_v6_Linux.tar.gz"
# el plugin precompilado sale de las releases de este repositorio, con su .sha256
# al lado (integridad de la descarga; la autenticidad la da el TLS de GitHub)
PLUGIN_URL="https://github.com/$REPO/releases/latest/download/libgstndi-x86_64.so"
GST_RS_TAG="0.12.11"      # versión (tag) de gst-plugins-rs para compilar el plugin NDI

ACEPTAR_NDI=0
RED=1
COMPILAR=0
for arg in "$@"; do
  case "$arg" in
    --accept-ndi-license) ACEPTAR_NDI=1 ;;
    --no-network) RED=0 ;;
    --build-plugin) COMPILAR=1 ;;
    -h|--help)
      cat <<'EOF'
NDIICAM installer / instalador

  --accept-ndi-license  accept the NDI SDK license without asking
                        acepta la licencia del NDI SDK sin preguntar
  --no-network          do not move the network to NetworkManager
                        no pasa la red a NetworkManager
  --build-plugin        build the GStreamer NDI plugin instead of downloading it
                        compila el plugin NDI de GStreamer en vez de descargarlo
EOF
      exit 0 ;;
    *) echo "Unknown option / opción desconocida: $arg" >&2; exit 1 ;;
  esac
done

paso()  { printf '\n\033[1;34m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
aviso() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
error() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || error "Run as root / ejecuta como root: sudo ./install.sh"
[ "$(uname -m)" = x86_64 ] || error "Only x86_64 (Intel/AMD) / solo x86_64 (Intel/AMD)"
command -v apt-get >/dev/null || error "Needs an apt-based distro (Ubuntu/Debian) / necesita Ubuntu o Debian"
command -v systemctl >/dev/null || error "Needs systemd / necesita systemd"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# origen de los ficheros: el repo clonado o, con 'curl | bash', una descarga
ORIGEN="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo .)"
if [ ! -f "$ORIGEN/ndiicam/ndiicam.py" ]; then
  paso "Downloading NDIICAM / descargando NDIICAM ($REPO, $RAMA)"
  apt-get install -y -qq curl ca-certificates >/dev/null
  curl -fsSL "https://github.com/$REPO/archive/refs/heads/$RAMA.tar.gz" | tar -xz -C "$TMP"
  ORIGEN="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
fi

# --------------------------------------------------------------- dependencias
paso "Installing dependencies / instalando dependencias"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# gstreamer1.0-plugins-bad trae kmssink: la salida HDMI (encuadre y modo receptor)
# gstreamer1.0-libav: el decodificador H.264 de FFmpeg para el modo puente
apt-get install -y -qq --no-install-recommends \
  python3 python3-gi python3-yaml gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-alsa \
  gstreamer1.0-plugins-bad gstreamer1.0-libav \
  python3-pil python3-segno fonts-dejavu-core \
  v4l-utils network-manager dnsmasq-base hostapd iw avahi-daemon avahi-utils \
  iproute2 curl ca-certificates nftables openssl >/dev/null
# GPU Intel: driver VA moderno (iHD) para la salida H.264 por GPU. El antiguo
# (i965) no entrega decodificado el JPEG 4:2:0 de la mayoría de cámaras
REINICIO_HUC=0
if grep -qs 0x8086 /sys/class/drm/card*/device/vendor; then
  # El completo (non-free, en multiverse/non-free) trae además el procesador de
  # vídeo (VPP): con él la GPU decodifica el JPEG de las cámaras y el H.264 del
  # modo puente y los deja listos, y la CPU apenas trabaja. Si no está, el libre
  if apt-cache show intel-media-va-driver-non-free >/dev/null 2>&1; then
    apt-get install -y -qq --no-install-recommends intel-media-va-driver-non-free >/dev/null \
      || aviso "intel-media-va-driver-non-free not installed / no instalado"
  fi
  dpkg -s intel-media-va-driver-non-free >/dev/null 2>&1 \
    || apt-get install -y -qq --no-install-recommends intel-media-va-driver >/dev/null \
    || aviso "intel-media-va-driver not installed: no GPU output / sin salida por GPU"
  # GStreamer guarda qué elementos VA hay: con otro driver hay que volver a mirarlo
  rm -rf /root/.cache/gstreamer-1.0 /var/lib/ndiicam/.cache/gstreamer-1.0
  # Las Intel Gen9 (Skylake a Gemini Lake) traen HuC pero el kernel no lo carga.
  # Sin él, su codificador de bajo consumo puede colgar el equipo entero (NDIICAM
  # no lo usa sin HuC: la salida GPU queda desactivada hasta reiniciar)
  # sin tubería: con pipefail, 'cat | head' falla al azar si cat sigue escribiendo
  HUC="$(cat /sys/kernel/debug/dri/*/gt*/uc/huc_info 2>/dev/null || true)"
  if [ -n "$HUC" ] && ! grep -qi "not supported" <<<"$HUC" \
      && ! grep -qs "enable_guc" /etc/default/grub /etc/default/grub.d/*.cfg \
      && [ -d /etc/default/grub.d ] && command -v update-grub >/dev/null; then
    printf '%s\n' "# NDIICAM: carga el firmware HuC de la GPU Intel (salida H.264 por GPU)" \
      'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT i915.enable_guc=2"' \
      > /etc/default/grub.d/80-ndiicam-huc.cfg
    update-grub >/dev/null 2>&1 && REINICIO_HUC=1
  fi
fi
# la consola de texto de tty1 deja sitio a la pantalla de ayuda por HDMI;
# Alt+F2 sigue abriendo una consola (autovt)
systemctl disable --now getty@tty1.service >/dev/null 2>&1 || true
# hostapd lo arranca NDIICAM cuando hace falta, no su servicio
systemctl disable --now hostapd >/dev/null 2>&1 || true
systemctl mask hostapd >/dev/null 2>&1 || true
systemctl enable --now avahi-daemon >/dev/null 2>&1

# -------------------------------------------------------------------- NDI SDK
# grep sin -q: con pipefail, si grep corta antes de que ldconfig acabe, la tubería falla
if ldconfig -p | grep 'libndi\.so\.6' >/dev/null; then
  paso "NDI SDK already installed / NDI SDK ya instalado"
else
  paso "NDI® SDK"
  cat <<'EOF'
NDIICAM needs the NDI® SDK runtime by Vizrt NDI AB. It is proprietary software:
it is downloaded from the official NDI servers and its license applies.
NDIICAM necesita el runtime del NDI® SDK de Vizrt NDI AB. Es software privativo:
se descarga de los servidores oficiales de NDI y se rige por su licencia.

  License / licencia: https://ndi.video/sdk/  (also shown by the SDK installer)
EOF
  if [ "$ACEPTAR_NDI" -ne 1 ]; then
    [ -r /dev/tty ] || error "No terminal: use --accept-ndi-license / sin terminal: usa --accept-ndi-license"
    read -r -p "Accept the NDI SDK license? / ¿Aceptas la licencia del NDI SDK? [y/N] " r </dev/tty
    [[ "$r" =~ ^[yYsS] ]] || error "The NDI SDK license is required / hace falta aceptar la licencia del NDI SDK"
  fi
  curl -fL --progress-bar "$NDI_SDK_URL" -o "$TMP/ndi.tar.gz"
  tar -xzf "$TMP/ndi.tar.gz" -C "$TMP"
  # `yes` muere con SIGPIPE (141) cuando el instalador del SDK termina; con
  # pipefail eso abortaba una instalación limpia aunque el SDK quedara bien.
  (cd "$TMP" && { yes || true; } | PAGER=cat bash ./Install_NDI_SDK_v6_Linux.sh >/dev/null)
  LIB="$(ls "$TMP/NDI SDK for Linux/lib/x86_64-linux-gnu/"libndi.so.6.* | head -1)"
  [ -f "$LIB" ] || error "NDI SDK: libndi.so.6 not found / no se encontró libndi.so.6"
  install -m 755 "$LIB" /usr/local/lib/
  ln -sf "$(basename "$LIB")" /usr/local/lib/libndi.so.6
  ln -sf libndi.so.6 /usr/local/lib/libndi.so
  ldconfig
fi

# ------------------------------------- MediaMTX (modo puente, como un ATEM Streaming Bridge)
# Servidor RTMP/SRT donde publica el ATEM; GStreamer no sabe hacer de servidor.
# MIT, un solo binario. Versión fijada y comprobada con su SHA-256
MEDIAMTX_VER="v1.21.2"
MEDIAMTX_SHA="121be6e00397e473a95d83c75f4fcd3819a6af3b0e1a50889cba43ab4fab6a27"
MEDIAMTX_BIN="/usr/local/lib/ndiicam/mediamtx"
if [ "$("$MEDIAMTX_BIN" --version 2>/dev/null)" = "$MEDIAMTX_VER" ]; then
  paso "MediaMTX $MEDIAMTX_VER already installed / ya instalado"
else
  paso "MediaMTX $MEDIAMTX_VER (bridge mode / modo puente, MIT)"
  if curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/$MEDIAMTX_VER/mediamtx_${MEDIAMTX_VER}_linux_amd64.tar.gz" \
       -o "$TMP/mediamtx.tgz" \
     && echo "$MEDIAMTX_SHA  $TMP/mediamtx.tgz" | sha256sum -c --quiet; then
    tar -xzf "$TMP/mediamtx.tgz" -C "$TMP" mediamtx
    install -d -m 755 "$(dirname "$MEDIAMTX_BIN")"
    install -m 755 "$TMP/mediamtx" "$MEDIAMTX_BIN"
  else
    aviso "MediaMTX not installed: no bridge mode / sin modo puente"
  fi
fi

# ------------------------------------------------- plugin NDI de GStreamer
DIR_GST="$(dirname "$(gst-inspect-1.0 coreelements | awk '/Filename/ {print $2}')")"
# vale el que haya: sin el parche de tally (plugin/) NDIICAM funciona igual, solo
# no enseña el tally. Forzar el nuestro obligaría a compilar Rust en el equipo
if gst-inspect-1.0 ndisink >/dev/null 2>&1; then
  paso "GStreamer NDI plugin already installed / plugin NDI de GStreamer ya instalado"
else
  paso "GStreamer NDI plugin / plugin NDI de GStreamer (gst-plugins-rs, MPL-2.0)"
  if [ "$COMPILAR" -eq 0 ] && curl -fsSL "$PLUGIN_URL" -o "$TMP/libgstndi.so" \
      && curl -fsSL "$PLUGIN_URL.sha256" -o "$TMP/libgstndi.sha256" \
      && (cd "$TMP" && echo "$(cut -d' ' -f1 libgstndi.sha256)  libgstndi.so" | sha256sum -c --quiet); then
    echo "Prebuilt plugin downloaded and verified / plugin precompilado descargado y verificado"
  else
    aviso "Building the plugin from source: it can take a long time on slow CPUs"
    aviso "Compilando el plugin: en CPUs lentas puede tardar bastante"
    apt-get install -y -qq --no-install-recommends build-essential pkg-config git patch \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev >/dev/null
    export RUSTUP_HOME="$TMP/rustup" CARGO_HOME="$TMP/cargo"
    curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --no-modify-path >/dev/null
    git clone -q --depth 1 --branch "$GST_RS_TAG" \
      https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git "$TMP/gst-plugins-rs"
    # tally y número de receptores del emisor (plugin/ndisink-tally.patch)
    (cd "$TMP/gst-plugins-rs" && patch -p1 < "$ORIGEN/plugin/ndisink-tally.patch" \
      && patch -p1 < "$ORIGEN/plugin/ndisinkcombiner-baja-latencia.patch" \
      && "$CARGO_HOME/bin/cargo" build --release --locked -p gst-plugin-ndi)
    cp "$TMP/gst-plugins-rs/target/release/libgstndi.so" "$TMP/libgstndi.so"
  fi
  install -m 644 "$TMP/libgstndi.so" "$DIR_GST/libgstndi.so"
  rm -rf /root/.cache/gstreamer-1.0 /var/lib/ndiicam/.cache/gstreamer-1.0
  gst-inspect-1.0 ndisink >/dev/null 2>&1 || error "The NDI plugin does not load / el plugin NDI no carga"
fi

# -------------------------------------------------------------------- NDIICAM
paso "Installing NDIICAM / instalando NDIICAM"
for puerto in 80 443; do
  for pid in $(ss -ltnpH "sport = :$puerto" | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u); do
    if ! tr '\0' ' ' <"/proc/$pid/cmdline" | grep -q ndiicam.py; then
      aviso "Port $puerto is in use by another program: the panel will not start until it is free"
      aviso "El puerto $puerto lo usa otro programa: el panel no arrancará hasta que quede libre"
      ss -ltnpH "sport = :$puerto" >&2 || true
      break
    fi
  done
done
install -d -m 755 /opt/ndiicam /opt/ndiicam/web
# el servicio son varios módulos (ver la cabecera de ndiicam.py): van todos
install -m 755 "$ORIGEN"/ndiicam/*.py /opt/ndiicam/
install -m 755 "$ORIGEN/system/ndiicam-watchdog.sh" /opt/ndiicam/
install -m 644 "$ORIGEN/ndiicam/web/index.html" /opt/ndiicam/web/
install -m 644 "$ORIGEN/system/ndiicam.service" /etc/systemd/system/ndiicam.service
install -d -m 755 /etc/avahi/services
install -m 644 "$ORIGEN/system/ndiicam-avahi.service" /etc/avahi/services/ndiicam.service
install -d -m 755 /etc/NetworkManager/conf.d
# "zz-" para que se lea después de default-wifi-powersave-on.conf de Ubuntu:
# NetworkManager aplica los conf.d por orden alfabético y gana el último.
rm -f /etc/NetworkManager/conf.d/90-ndiicam.conf
install -m 644 "$ORIGEN/system/zz-ndiicam.conf" /etc/NetworkManager/conf.d/zz-ndiicam.conf
install -d -m 700 /etc/ndiicam /var/lib/ndiicam
systemctl reload NetworkManager >/dev/null 2>&1 || true
# la nueva configuración vale para las próximas conexiones; en la activa se
# apaga ya el ahorro de energía, sin cortar la WiFi
for w in /sys/class/net/*/wireless; do
  [ -e "$w" ] && iw dev "$(basename "$(dirname "$w")")" set power_save off 2>/dev/null || true
done

# fiabilidad: el equipo tiene que volver solo si se cuelga, sin nadie delante
#  - watchdog de la placa, alimentado por systemd (30 s)
#  - reinicio a los 10 s tras un fallo del kernel
#  - registro en disco, para saber después qué pasó
install -m 644 "$ORIGEN/system/ndiicam-watchdog.service" /etc/systemd/system/
install -d -m 755 /etc/systemd/system.conf.d /etc/systemd/journald.conf.d /etc/sysctl.d
install -m 644 "$ORIGEN/system/zz-ndiicam-watchdog.conf" /etc/systemd/system.conf.d/
install -m 644 "$ORIGEN/system/90-ndiicam-panico.conf" /etc/sysctl.d/
install -m 644 "$ORIGEN/system/ndiicam-journald.conf" /etc/systemd/journald.conf.d/ndiicam.conf
sysctl -q -p /etc/sysctl.d/90-ndiicam-panico.conf || true
systemctl restart systemd-journald >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl enable ndiicam-watchdog.service >/dev/null 2>&1
systemctl start ndiicam-watchdog.service >/dev/null 2>&1 || true
# systemd lee RuntimeWatchdogSec al arrancar: reexec lo aplica sin reiniciar
systemctl daemon-reexec >/dev/null 2>&1 || true
if [ -e /dev/watchdog0 ]; then
  echo "Hardware watchdog active / watchdog de la placa activo: $(cat /sys/class/watchdog/watchdog0/identity 2>/dev/null)"
else
  aviso "No hardware watchdog found: a hang will need a manual reboot / sin watchdog: un cuelgue necesitará reinicio a mano"
fi

systemctl enable ndiicam.service >/dev/null 2>&1
systemctl restart ndiicam.service
sleep 4
systemctl is-active --quiet ndiicam.service || {
  journalctl -u ndiicam.service -n 20 --no-pager >&2
  error "The service did not start / el servicio no arrancó"
}

EQUIPO="$(hostname)"
IP="$(ip -4 -o route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="src") print $(i+1)}')"
# la clave de la WiFi de configuración es propia de cada equipo (el servicio la
# genera al arrancar); también sale en la pantalla de ayuda HDMI y en el panel
AP_CLAVE="$(python3 -c 'import json; print(json.load(open("/etc/ndiicam/config.json")).get("ap_clave", ""))' 2>/dev/null || true)"
cat <<EOF

  NDIICAM is installed / NDIICAM está instalado

  Panel:        https://$EQUIPO.local   ${IP:+(https://$IP)}
                the first visit asks you to create a password / la primera visita pide crear una contraseña
                the browser warns about the certificate (it is the device's own) / el navegador
                avisa del certificado (es propio del equipo): acéptalo
  NDI source:   ${EQUIPO^^} (NDIICAM)
  Setup WiFi:   turns on when no known network is available / se activa sin red conocida
                password / clave: ${AP_CLAVE:-(see the panel or the HDMI help screen / ver el panel o la pantalla HDMI)}
                (unique to this device; change it in the panel / propia de este equipo; cámbiala en el panel)

EOF
if [ "$REINICIO_HUC" -eq 1 ]; then
  aviso "Reboot to enable the GPU H.264 output (HuC firmware) / reinicia para activar la salida H.264 por GPU (firmware HuC)"
fi

# ----------------------------------------------------------------------- red
if [ "$RED" -eq 1 ]; then
  paso "Network: NetworkManager / red: NetworkManager"
  echo "The network may drop for a few seconds; if it does not come back in 90 s it is restored."
  echo "La red puede cortarse unos segundos; si no vuelve en 90 s, se restaura sola."
  python3 /opt/ndiicam/red_nm.py preparar \
    || aviso "Network unchanged: see docs/network.md / red sin cambios: ver docs/network.md"
fi
