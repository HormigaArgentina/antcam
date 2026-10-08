#!/bin/bash
# Copia AntCam y sus ajustes de sistema dentro de un sistema de archivos raíz.
# Lo usan install.sh (RAIZ=/) y la fábrica de imágenes (RAIZ=$ROOTFS_DIR).
# Uso: instalar_archivos.sh RAIZ CARPETA_DEL_REPO
set -e
RAIZ="${1:?falta RAIZ}"
SRC="${2:?falta carpeta del repo}"

# programa
rm -rf "$RAIZ/opt/antcam.nuevo"
mkdir -p "$RAIZ/opt/antcam.nuevo"
cp -r "$SRC/antcam" "$RAIZ/opt/antcam.nuevo/"
find "$RAIZ/opt/antcam.nuevo" -name __pycache__ -prune -exec rm -rf {} +
rm -rf "$RAIZ/opt/antcam"
mv "$RAIZ/opt/antcam.nuevo" "$RAIZ/opt/antcam"
mkdir -p "$RAIZ/var/lib/antcam" "$RAIZ/media/antcam"

# servicios
install -m 644 "$SRC"/systemd/antcam-*.service "$RAIZ/etc/systemd/system/"

# red propia: el celular abre la página solo al conectarse (portal cautivo)
mkdir -p "$RAIZ/etc/NetworkManager/dnsmasq-shared.d"
echo 'address=/#/10.42.0.1' > "$RAIZ/etc/NetworkManager/dnsmasq-shared.d/antcam.conf"

# reinicio automático si el sistema se cuelga; registros acotados
mkdir -p "$RAIZ/etc/systemd/system.conf.d" "$RAIZ/etc/systemd/journald.conf.d"
printf '[Manager]\nRuntimeWatchdogSec=15\n' > "$RAIZ/etc/systemd/system.conf.d/antcam-watchdog.conf"
printf '[Journal]\nSystemMaxUse=50M\n' > "$RAIZ/etc/systemd/journald.conf.d/antcam.conf"

# config.txt: cámara y Bluetooth apagado (ahorra energía)
CFGTXT="$RAIZ/boot/firmware/config.txt"
[ -f "$CFGTXT" ] || CFGTXT="$RAIZ/boot/config.txt"
if [ -f "$CFGTXT" ]; then
  grep -q '^camera_auto_detect=1' "$CFGTXT" || echo 'camera_auto_detect=1' >> "$CFGTXT"
  grep -q '^dtoverlay=disable-bt' "$CFGTXT" || echo 'dtoverlay=disable-bt' >> "$CFGTXT"
fi

# archivo de ajustes editable desde Windows (no se pisa si ya existe)
BOOT="$RAIZ/boot/firmware"
[ -d "$BOOT" ] || BOOT="$RAIZ/boot"
[ -f "$BOOT/antcam.txt" ] || sed 's/$/\r/' "$SRC/sistema/antcam.txt" > "$BOOT/antcam.txt"
