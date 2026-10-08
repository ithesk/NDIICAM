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
NDI_SDK_URL="https://downloads.ndi.tv/SDK/NDI_SDK_Linux/Install_NDI_SDK_v6_Linux.tar.gz"
PLUGIN_URL="https://github.com/$REPO/releases/latest/download/libgstndi-x86_64.so"
GST_RS_TAG="0.12.11"      # versión de gst-plugins-rs para compilar el plugin NDI

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
apt-get install -y -qq --no-install-recommends \
  python3 python3-gi python3-yaml gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-alsa \
  v4l-utils network-manager dnsmasq-base hostapd iw avahi-daemon avahi-utils \
  iproute2 curl ca-certificates >/dev/null
# hostapd lo arranca NDIICAM cuando hace falta, no su servicio
systemctl disable --now hostapd >/dev/null 2>&1 || true
systemctl mask hostapd >/dev/null 2>&1 || true
systemctl enable --now avahi-daemon >/dev/null 2>&1

# -------------------------------------------------------------------- NDI SDK
if ldconfig -p | grep -q 'libndi\.so\.6'; then
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
  (cd "$TMP" && yes | PAGER=cat bash ./Install_NDI_SDK_v6_Linux.sh >/dev/null)
  LIB="$(ls "$TMP/NDI SDK for Linux/lib/x86_64-linux-gnu/"libndi.so.6.* | head -1)"
  [ -f "$LIB" ] || error "NDI SDK: libndi.so.6 not found / no se encontró libndi.so.6"
  install -m 755 "$LIB" /usr/local/lib/
  ln -sf "$(basename "$LIB")" /usr/local/lib/libndi.so.6
  ln -sf libndi.so.6 /usr/local/lib/libndi.so
  ldconfig
fi

# ------------------------------------------------- plugin NDI de GStreamer
DIR_GST="$(dirname "$(gst-inspect-1.0 coreelements | awk '/Filename/ {print $2}')")"
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
    apt-get install -y -qq --no-install-recommends build-essential pkg-config git \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev >/dev/null
    export RUSTUP_HOME="$TMP/rustup" CARGO_HOME="$TMP/cargo"
    curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --no-modify-path >/dev/null
    git clone -q --depth 1 --branch "$GST_RS_TAG" \
      https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git "$TMP/gst-plugins-rs"
    (cd "$TMP/gst-plugins-rs" && "$CARGO_HOME/bin/cargo" build --release --locked -p gst-plugin-ndi)
    cp "$TMP/gst-plugins-rs/target/release/libgstndi.so" "$TMP/libgstndi.so"
  fi
  install -m 644 "$TMP/libgstndi.so" "$DIR_GST/libgstndi.so"
  rm -rf /root/.cache/gstreamer-1.0 /var/lib/ndiicam/.cache/gstreamer-1.0
  gst-inspect-1.0 ndisink >/dev/null 2>&1 || error "The NDI plugin does not load / el plugin NDI no carga"
fi

# -------------------------------------------------------------------- NDIICAM
paso "Installing NDIICAM / instalando NDIICAM"
for pid in $(ss -ltnpH 'sport = :80' | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u); do
  if ! tr '\0' ' ' <"/proc/$pid/cmdline" | grep -q ndiicam.py; then
    aviso "Port 80 is in use by another program: the panel will not start until it is free"
    aviso "El puerto 80 lo usa otro programa: el panel no arrancará hasta que quede libre"
    ss -ltnpH 'sport = :80' >&2 || true
    break
  fi
done
install -d -m 755 /opt/ndiicam /opt/ndiicam/web
install -m 755 "$ORIGEN/ndiicam/ndiicam.py" "$ORIGEN/ndiicam/fuente.py" "$ORIGEN/ndiicam/red_nm.py" /opt/ndiicam/
install -m 644 "$ORIGEN/ndiicam/web/index.html" /opt/ndiicam/web/
install -m 644 "$ORIGEN/system/ndiicam.service" /etc/systemd/system/ndiicam.service
install -d -m 755 /etc/avahi/services
install -m 644 "$ORIGEN/system/ndiicam-avahi.service" /etc/avahi/services/ndiicam.service
install -d -m 755 /etc/NetworkManager/conf.d
install -m 644 "$ORIGEN/system/90-ndiicam.conf" /etc/NetworkManager/conf.d/90-ndiicam.conf
install -d -m 700 /etc/ndiicam /var/lib/ndiicam
systemctl reload NetworkManager >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl enable ndiicam.service >/dev/null 2>&1
systemctl restart ndiicam.service
sleep 4
systemctl is-active --quiet ndiicam.service || {
  journalctl -u ndiicam.service -n 20 --no-pager >&2
  error "The service did not start / el servicio no arrancó"
}

EQUIPO="$(hostname)"
IP="$(ip -4 -o route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="src") print $(i+1)}')"
cat <<EOF

  NDIICAM is installed / NDIICAM está instalado

  Panel:        http://$EQUIPO.local   ${IP:+(http://$IP)}
  NDI source:   ${EQUIPO^^} (NDIICAM)
  Setup WiFi:   turns on when no known network is available / se activa sin red conocida
                password / clave: ndiicam-setup  (change it in the panel / cámbiala en el panel)

EOF

# ----------------------------------------------------------------------- red
if [ "$RED" -eq 1 ]; then
  paso "Network: NetworkManager / red: NetworkManager"
  echo "The network may drop for a few seconds; if it does not come back in 90 s it is restored."
  echo "La red puede cortarse unos segundos; si no vuelve en 90 s, se restaura sola."
  python3 /opt/ndiicam/red_nm.py preparar \
    || aviso "Network unchanged: see docs/network.md / red sin cambios: ver docs/network.md"
fi
