#!/bin/sh
# NDIICAM — carga el watchdog de la placa para que systemd lo alimente
# (RuntimeWatchdogSec). Ubuntu tiene estos módulos en la lista negra de kmod,
# así que modules-load.d no los carga: hay que pedirlos por su nombre.
# Si el equipo se congela entero, la placa lo reinicia sola.
[ -e /dev/watchdog0 ] && exit 0
# ACPI WDAT: el que anuncia la BIOS (Apollo Lake, Gemini Lake y muchos más)
[ -e /sys/firmware/acpi/tables/WDAT ] && modprobe wdat_wdt 2>/dev/null
# el TCO del chipset Intel y su equivalente en AMD
[ -e /dev/watchdog0 ] || modprobe iTCO_wdt 2>/dev/null
[ -e /dev/watchdog0 ] || modprobe sp5100_tco 2>/dev/null
exit 0
