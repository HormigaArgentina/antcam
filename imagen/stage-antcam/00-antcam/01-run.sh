#!/bin/bash -e
# Etapa AntCam de pi-gen: instala AntCam sobre Raspberry Pi OS Lite.
# La carpeta files/repo la copia el workflow de GitHub antes de construir.
bash files/repo/sistema/instalar_archivos.sh "${ROOTFS_DIR}" "$(pwd)/files/repo"

on_chroot << CHROOT
systemctl enable NetworkManager avahi-daemon
systemctl enable antcam-arranque.service antcam-grabador.service antcam-web.service
systemctl disable hciuart.service 2>/dev/null || true
rm -f /var/lib/antcam/config.json
CHROOT
