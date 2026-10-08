#!/bin/bash
# Instalación manual de AntCam sobre Raspberry Pi OS Lite (Bookworm o más nuevo).
# (La forma fácil es grabar la imagen lista de AntCam: ver README.)
# Uso:   sudo ./install.sh [NombreDelEquipo]
# Se puede volver a ejecutar para actualizar: la configuración se conserva.
set -e

if [ "$EUID" -ne 0 ]; then
  echo "Ejecutalo con sudo:  sudo ./install.sh"
  exit 1
fi

DIR="$(cd "$(dirname "$0")" && pwd)"
NOMBRE="$1"
paso() { echo; echo "==> $*"; }

. /etc/os-release
echo "AntCam - instalación en: $PRETTY_NAME ($(uname -m))"
if ! systemctl list-unit-files NetworkManager.service >/dev/null 2>&1; then
  echo "Este sistema no usa NetworkManager. Instalá Raspberry Pi OS Bookworm o más nuevo."
  exit 1
fi

paso "1/5 Instalando programas (tarda unos minutos)"
apt-get update
apt-get install -y --no-install-recommends $(cat "$DIR/sistema/paquetes.txt")

paso "2/5 Copiando AntCam y ajustes del sistema"
bash "$DIR/sistema/instalar_archivos.sh" / "$DIR"
systemctl disable --now hciuart.service 2>/dev/null || true

paso "3/5 Nombre del equipo"
cd /opt/antcam
python3 - "$NOMBRE" <<'EOF'
import sys
from antcam import config, arranque
cfg = config.load()
if sys.argv[1]:
    cfg["nombre"] = sys.argv[1]
cfg = config.save(cfg)
arranque.set_hostname(cfg["nombre"])
print("Equipo:", cfg["nombre"], " -> http://%s.local" % config.slug(cfg["nombre"]))
EOF
HOST=$(hostname)

paso "4/5 Zona horaria y país del WiFi"
timedatectl set-timezone America/Argentina/Buenos_Aires || true
if command -v raspi-config >/dev/null; then
  CUR=$(raspi-config nonint get_wifi_country 2>/dev/null || true)
  [ -z "$CUR" ] && raspi-config nonint do_wifi_country AR || true
fi
rfkill unblock wifi 2>/dev/null || true

paso "5/5 Servicios que arrancan solos al encender"
systemctl daemon-reload
systemctl enable NetworkManager avahi-daemon >/dev/null 2>&1 || true
systemctl restart avahi-daemon || true
systemctl enable antcam-arranque.service antcam-grabador.service antcam-web.service
systemctl restart antcam-grabador.service antcam-web.service

echo
echo "=========================================================="
echo " AntCam instalado."
echo
echo " La primera vez REINICIÁ para aplicar los ajustes:  sudo reboot"
echo
echo " Después abrí en el navegador (misma red WiFi):"
echo "     http://$HOST.local"
echo " Sin WiFi conocida, el equipo crea su propia red con su nombre"
echo " (clave por defecto: hormigas2026) y se entra a http://10.42.0.1"
echo "=========================================================="
