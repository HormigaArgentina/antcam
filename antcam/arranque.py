"""Ajustes al encender (antcam-arranque).

1. Primer encendido: si el equipo no tiene nombre, toma uno único del número de serie de la
   placa (ej. AntCam-3F2A), así varios equipos en la misma oficina no se confunden.
2. Lee el archivo antcam.txt de la tarjeta (se edita desde Windows con el Bloc de notas)
   y aplica lo que haya cambiado: nombre, redes WiFi, clave de la red propia, Telegram y
   la marca del video (lugar, especie, nota, fecha).
"""

import hashlib
import os
import re
import subprocess
from pathlib import Path

from . import config, events, paths

SRC = "arranque"
TXT = Path(os.environ.get("ANTCAM_TXT", "/boot/firmware/antcam.txt"))
APLICADO = paths.STATE_DIR / "antcam_txt.sha1"


def serial_suffix():
    for p in ("/sys/firmware/devicetree/base/serial-number",):
        try:
            s = Path(p).read_text().strip("\x00\n ")
            if s:
                return s[-4:].upper()
        except OSError:
            pass
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("serial"):
                return line.split(":")[1].strip()[-4:].upper()
    except OSError:
        pass
    return "0001"


def leer_txt(path):
    raw = path.read_bytes().decode("utf-8-sig", errors="replace")  # el Bloc de notas puede agregar BOM
    datos = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        datos[k.strip().lower()] = v.strip().strip('"')
    return datos


def set_hostname(nombre):
    host = config.slug(nombre)
    if paths.SIM:
        print("SIM hostname:", host)
        return
    subprocess.run(["hostnamectl", "set-hostname", host], capture_output=True)
    try:
        hosts = [l for l in Path("/etc/hosts").read_text().splitlines() if not l.startswith("127.0.1.1")]
        hosts.append(f"127.0.1.1\t{host}")
        Path("/etc/hosts").write_text("\n".join(hosts) + "\n")
    except OSError:
        pass


def main():
    paths.ensure_dirs()
    primera_vez = not paths.CONFIG_FILE.exists()
    cfg = config.load()

    datos = {}
    huella = None
    if TXT.exists():
        try:
            contenido = TXT.read_bytes()
            huella = hashlib.sha1(contenido).hexdigest()
            previo = APLICADO.read_text().strip() if APLICADO.exists() else ""
            if huella != previo:
                datos = leer_txt(TXT)
        except OSError:
            pass

    cambios = []
    nombre = datos.get("nombre", "")
    if nombre:
        cfg["nombre"] = nombre
        cambios.append(f"nombre {nombre}")
    elif primera_vez:
        cfg["nombre"] = f"AntCam-{serial_suffix()}"
        cambios.append(f"nombre automático {cfg['nombre']}")

    if datos.get("red_propia_clave"):
        cfg["wifi"]["ap_clave"] = datos["red_propia_clave"]
        cambios.append("clave de la red propia")
    ra = datos.get("red_propia_abierta", "").lower()
    if ra in ("si", "sí", "no"):
        cfg["wifi"]["ap_abierta"] = ra != "no"
        cambios.append(f"red propia abierta: {ra}")
    if datos.get("telegram_token"):
        cfg["avisos"]["telegram_token"] = datos["telegram_token"]
        cambios.append("token de Telegram")
    if datos.get("telegram_chat_id"):
        cfg["avisos"]["telegram_chat_id"] = datos["telegram_chat_id"]

    # marca en el video
    for k in ("lugar", "especie", "nota"):
        if datos.get(k):
            cfg["rotulo"][k] = datos[k]
            cambios.append(f"{k} {datos[k]}")
    mf = datos.get("marca_fecha", "").lower()
    if mf in ("si", "sí", "no"):
        cfg["rotulo"]["fecha_hora"] = mf != "no"
        cambios.append(f"fecha en el video: {mf}")

    viejo_nombre = config.load()["nombre"]
    cfg = config.save(cfg)
    if cfg["nombre"] != viejo_nombre or primera_vez:
        set_hostname(cfg["nombre"])

    # redes WiFi: wifi_nombre / wifi_clave, wifi_nombre2 / wifi_clave2, ...
    redes = []
    for k, v in datos.items():
        m = re.fullmatch(r"wifi_nombre(\d*)", k)
        if m and v:
            redes.append((v, datos.get(f"wifi_clave{m.group(1)}", "")))
    if redes:
        if not paths.SIM:
            subprocess.run(["nm-online", "-s", "-q", "--timeout=30"], capture_output=True)
        from .network import Network
        net = Network()
        for ssid, clave in redes:
            try:
                net.add(ssid, clave)
                cambios.append(f"WiFi {ssid}")
            except Exception as e:
                events.log("error", f"antcam.txt: no se pudo guardar la WiFi {ssid}: {e}", SRC)

    if huella:
        APLICADO.write_text(huella)
    if cambios:
        events.log("info", "Ajustes aplicados al encender: " + ", ".join(cambios), SRC)


if __name__ == "__main__":
    main()
