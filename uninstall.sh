#!/usr/bin/env bash
# NDIICAM — desinstalador.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
#
#   sudo ./uninstall.sh [--restore-network] [--purge]
#
#   --restore-network  devuelve la red a netplan/systemd-networkd como estaba
#   --purge            borra también la configuración, el NDI SDK y el plugin NDI
set -euo pipefail

RESTAURAR=0
PURGAR=0
for arg in "$@"; do
  case "$arg" in
    --restore-network) RESTAURAR=1 ;;
    --purge) PURGAR=1 ;;
    *) echo "Unknown option / opción desconocida: $arg" >&2; exit 1 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "Run as root / ejecuta como root" >&2; exit 1; }

systemctl disable --now ndiicam.service 2>/dev/null || true
if [ "$RESTAURAR" -eq 1 ]; then
  if [ -d /var/lib/ndiicam/netplan-respaldo ]; then
    python3 /opt/ndiicam/red_nm.py revertir
  else
    echo "No network backup found / no hay copia de la red" >&2
  fi
fi
rm -f /etc/systemd/system/ndiicam.service /etc/avahi/services/ndiicam.service \
      /etc/NetworkManager/conf.d/90-ndiicam.conf
rm -rf /opt/ndiicam /run/ndiicam
systemctl daemon-reload
systemctl reload NetworkManager 2>/dev/null || true
systemctl unmask hostapd 2>/dev/null || true

if [ "$PURGAR" -eq 1 ]; then
  rm -rf /etc/ndiicam /var/lib/ndiicam
  rm -f /usr/local/lib/libndi.so /usr/local/lib/libndi.so.6 /usr/local/lib/libndi.so.6.*
  ldconfig
  DIR_GST="$(dirname "$(gst-inspect-1.0 coreelements | awk '/Filename/ {print $2}')")"
  rm -f "$DIR_GST/libgstndi.so"
fi
echo "NDIICAM removed / NDIICAM desinstalado"
