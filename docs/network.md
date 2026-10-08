# Network / Red

NDIICAM needs **NetworkManager** to manage the network (WiFi scanning, saved networks and
the setup WiFi). The installer moves netplan + systemd-networkd setups (Ubuntu Server) by
itself, with an automatic rollback.

NDIICAM necesita que la red la gestione **NetworkManager** (buscar WiFi, redes guardadas y
WiFi de configuración). El instalador pasa solo las instalaciones con netplan +
systemd-networkd (Ubuntu Server), con vuelta atrás automática.

## Desktop systems / Sistemas de escritorio

Already on NetworkManager: nothing to do.
Ya usan NetworkManager: no hay nada que hacer.

## Debian with ifupdown (`/etc/network/interfaces`) / Debian con ifupdown

NetworkManager ignores interfaces listed in `/etc/network/interfaces`. From a local console
(not over SSH), comment out the WiFi/Ethernet stanzas, leaving only `lo`, then:

NetworkManager ignora las interfaces que aparecen en `/etc/network/interfaces`. Desde una
consola local (no por SSH), comenta las de WiFi/Ethernet, deja solo `lo`, y después:

```bash
sudo systemctl restart NetworkManager
sudo nmcli dev wifi connect "<SSID>" password "<password>"
```

## Rollback / Vuelta atrás

```bash
sudo python3 /opt/ndiicam/red_nm.py revertir
```

Restores the netplan files saved in `/var/lib/ndiicam/netplan-respaldo` and goes back to
systemd-networkd. Log: `/var/lib/ndiicam/red.log`.

Restaura los ficheros de netplan guardados en `/var/lib/ndiicam/netplan-respaldo` y vuelve
a systemd-networkd. Registro: `/var/lib/ndiicam/red.log`.

## Finding the device / Encontrar el equipo

- `http://<hostname>.local` (mDNS)
- From the router's DHCP list / en la lista DHCP del router
- From the setup WiFi: `http://10.42.0.1` / desde la WiFi de configuración
