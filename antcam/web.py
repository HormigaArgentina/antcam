"""Servicio web (antcam-web): página de configuración, WiFi automático y avisos.

Se abre desde el celular o la PC:
  - en red propia:      http://10.42.0.1
  - en la red de la oficina: http://<nombre>.local
"""

import ipaddress
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, request, send_file

from . import __version__, config, events, paths
from .network import AP_IP, Network
from .notifier import Notifier, detect_chat

SRC = "web"
HERE = Path(__file__).parent
app = Flask(__name__, static_folder=None)
net = Network()
notifier = Notifier()


# ------------------------------------------------------------------ utilidades
def send_cmd(name, arg=""):
    paths.CMD_DIR.mkdir(parents=True, exist_ok=True)
    f = paths.CMD_DIR / f"{name}.{time.time_ns()}"
    f.write_text(str(arg))


def run(cmd, timeout=20):
    if paths.SIM:
        print("SIM:", " ".join(cmd), flush=True)
        return 0, ""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def ntp_synced():
    if paths.SIM:
        return False
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return out == "yes"
    except Exception:
        return False


def has_rtc():
    return Path("/dev/rtc0").exists() and not paths.SIM


def recorder_status():
    try:
        with open(paths.STATUS_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    st["viejo"] = time.time() - st.get("actualizado", 0) > 15
    return st


def ok(**kw):
    return jsonify(dict(ok=True, **kw))


def fail(msg, code=400):
    return jsonify(ok=False, error=msg), code


# ------------------------------------------------------------------ acceso y portal cautivo
@app.before_request
def guard():
    host = (request.host or "").split(":")[0]
    if net.mode == "propia" and host and host != AP_IP and not host.endswith(".local"):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # el celular pregunta por google.com/apple.com al conectarse: lo mandamos a la página
            return redirect(f"http://{AP_IP}/", code=302)
    clave = config.load()["acceso"]["clave"]
    if clave:
        a = request.authorization
        if not a or a.password != clave:
            return Response("Clave requerida", 401, {"WWW-Authenticate": 'Basic realm="AntCam"'})


# ------------------------------------------------------------------ página
@app.get("/")
def index():
    return send_file(HERE / "static" / "index.html", max_age=0)


@app.get("/<any('logo.png', 'hormiga_marca.png'):nombre>")
def imagen_estatica(nombre):
    return send_file(HERE / "static" / nombre, mimetype="image/png", max_age=86400)


@app.get("/vista.jpg")
def preview():
    t0 = time.time()
    paths.PREVIEW_REQ.touch()
    while time.time() - t0 < 2.5:
        try:
            if paths.PREVIEW_FILE.stat().st_mtime >= t0 - 1.0:
                break
        except OSError:
            pass
        time.sleep(0.1)
    if not paths.PREVIEW_FILE.exists():
        return Response(status=204)
    r = send_file(paths.PREVIEW_FILE, mimetype="image/jpeg", max_age=0)
    r.headers["Cache-Control"] = "no-store"
    return r


# ------------------------------------------------------------------ estado
@app.get("/api/estado")
def api_status():
    cfg = config.load()
    return jsonify(
        nombre=cfg["nombre"], version=__version__,
        grabador=recorder_status(),
        red=net.status(),
        hora={"pi": time.time(), "ntp": ntp_synced(), "rtc": has_rtc(),
              "ajustada_celular": paths.TIME_SYNC_FLAG.exists()},
        avisos=dict(notifier.info(), configurado=bool(cfg["avisos"]["telegram_token"]
                                                      and cfg["avisos"]["telegram_chat_id"])),
        tamano_salida=config.output_size(cfg),
    )


@app.get("/api/eventos")
def api_events():
    n = min(int(request.args.get("n", 60)), 300)
    return jsonify(events.tail(n, request.args.get("nivel", "info")))


# ------------------------------------------------------------------ hora
@app.post("/api/hora")
def api_time():
    data = request.get_json(force=True)
    phone = float(data.get("ms", 0)) / 1000
    diff = phone - time.time()
    adjusted = False
    if abs(diff) > 3 and not ntp_synced():
        code, out = run(["date", "-s", f"@{phone:.3f}"])
        if code == 0:
            adjusted = True
            run(["fake-hwclock", "save"])
            if has_rtc():
                run(["hwclock", "-w"])
            events.log("aviso" if abs(diff) > 300 else "info",
                       f"Hora ajustada con la del celular (estaba corrida {diff:+.0f} s)", SRC)
    if adjusted or abs(diff) <= 3:
        paths.TIME_SYNC_FLAG.touch()
    return ok(diferencia_s=round(diff, 1), ajustada=adjusted)


# ------------------------------------------------------------------ configuración
@app.get("/api/config")
def api_config_get():
    return jsonify(config.load())


@app.post("/api/config")
def api_config_set():
    data = request.get_json(force=True) or {}
    old = config.load()
    data.pop("acceso", None)  # la clave de acceso tiene su propio endpoint
    cfg = config.update(data)
    if cfg["nombre"] != old["nombre"]:
        threading.Thread(target=rename_device, args=(cfg["nombre"],), daemon=True).start()
    if cfg["wifi"] != old["wifi"] or cfg["nombre"] != old["nombre"]:
        try:
            net.ensure_ap_profile()
        except Exception:
            pass
    send_cmd("recargar")
    return ok(config=cfg)


def rename_device(nombre):
    host = config.slug(nombre)
    run(["hostnamectl", "set-hostname", host])
    if not paths.SIM:
        try:
            lines = Path("/etc/hosts").read_text().splitlines()
            lines = [l for l in lines if not l.startswith("127.0.1.1")]
            lines.append(f"127.0.1.1\t{host}")
            Path("/etc/hosts").write_text("\n".join(lines) + "\n")
        except OSError:
            pass
    run(["systemctl", "restart", "avahi-daemon"])
    events.log("info", f"Nombre del equipo cambiado a {nombre} (dirección http://{host}.local)", SRC)


@app.post("/api/clave_acceso")
def api_access():
    clave = (request.get_json(force=True) or {}).get("clave", "")
    config.update({"acceso": {"clave": clave}})
    return ok()


@app.post("/api/grabar")
def api_record():
    on = bool((request.get_json(force=True) or {}).get("on"))
    config.update({"grabacion": {"habilitada": on}})
    send_cmd("recargar")
    return ok()


@app.post("/api/encuadre")
def api_framing():
    on = bool((request.get_json(force=True) or {}).get("on"))
    send_cmd("encuadre_on" if on else "encuadre_off")
    return ok()


@app.post("/api/zona")
def api_zone():
    data = request.get_json(force=True) or {}
    upd = {"zona": data.get("zona")}
    if "rotar_180" in data:
        upd["rotar_180"] = bool(data["rotar_180"])
    cfg = config.update({"grabacion": upd})
    send_cmd("recargar")
    if data.get("terminar", True):
        send_cmd("encuadre_off")
    return ok(zona=cfg["grabacion"]["zona"], tamano=config.output_size(cfg))


@app.post("/api/expulsar")
def api_eject():
    send_cmd("expulsar", (request.get_json(force=True) or {}).get("id", ""))
    return ok()


@app.post("/api/prueba")
def api_test():
    send_cmd("prueba")
    return ok()


# ------------------------------------------------------------------ WiFi
@app.get("/api/wifi")
def api_wifi():
    scan = request.args.get("buscar") == "1"
    return jsonify(estado=net.status(), conocidas=net.known(),
                   visibles=net.scan_list() if scan else net.scan)


@app.post("/api/wifi/agregar")
def api_wifi_add():
    d = request.get_json(force=True) or {}
    try:
        net.add(d.get("ssid", ""), d.get("clave", ""))
    except Exception as e:
        return fail(str(e))
    return ok()


@app.post("/api/wifi/borrar")
def api_wifi_del():
    net.delete((request.get_json(force=True) or {}).get("nombre", ""))
    return ok()


@app.post("/api/wifi/probar")
def api_wifi_try():
    def go():
        if not net.try_known():
            net.start_ap()
    threading.Thread(target=go, daemon=True).start()
    return ok()


@app.post("/api/wifi/propia")
def api_wifi_ap():
    threading.Thread(target=net.start_ap, kwargs={"hold_min": 30}, daemon=True).start()
    return ok()


# ------------------------------------------------------------------ avisos
@app.post("/api/avisos/detectar")
def api_tg_detect():
    token = ((request.get_json(force=True) or {}).get("token") or "").strip()
    if not token:
        return fail("Pegá primero el token del bot")
    try:
        chat_id, name = detect_chat(token)
    except Exception as e:
        return fail(str(e))
    config.update({"avisos": {"telegram_token": token, "telegram_chat_id": chat_id}})
    return ok(chat_id=chat_id, nombre=name)


@app.post("/api/avisos/probar")
def api_tg_test():
    if not net.internet and not paths.SIM:
        return fail("El equipo no tiene internet ahora: conectalo a una red con internet "
                    "(por ejemplo, el hotspot de tu celular) y volvé a probar.")
    if notifier.test():
        return ok()
    return fail(notifier.last_error or "No se pudo enviar")


# ------------------------------------------------------------------ diagnóstico
DIAG_CMDS = [
    ("Sistema", ["sh", "-c", "cat /etc/os-release | head -2; uname -a; uptime; "
                             "cat /proc/device-tree/model 2>/dev/null; echo"]),
    ("Cámaras detectadas", ["rpicam-hello", "--list-cameras"]),
    ("Tensión / temperatura", ["sh", "-c", "vcgencmd get_throttled; vcgencmd measure_temp; vcgencmd measure_volts"]),
    ("Discos y pendrives", ["sh", "-c", "lsblk -o NAME,FSTYPE,LABEL,SIZE,MOUNTPOINT,TRAN; echo; df -h"]),
    ("Red", ["sh", "-c", "nmcli device status; echo; nmcli -t -f NAME,TYPE,AUTOCONNECT connection show; "
                         "echo; hostname -I"]),
    ("Hora", ["timedatectl"]),
    ("Servicios", ["systemctl", "--no-pager", "status", "antcam-grabador", "antcam-web", "antcam-arranque"]),
    ("Registro del grabador", ["journalctl", "--no-pager", "-n", "400", "-u", "antcam-grabador"]),
    ("Registro de la página / WiFi", ["journalctl", "--no-pager", "-n", "200", "-u", "antcam-web"]),
    ("Registro del arranque", ["journalctl", "--no-pager", "-n", "60", "-u", "antcam-arranque"]),
    ("Mensajes del sistema (cámara, USB, energía)", ["sh", "-c",
        "dmesg -T | grep -iE 'imx|unicam|camera|usb|voltage|error|fail' | tail -80"]),
]


@app.get("/energia.csv")
def energia_csv():
    if not paths.ENERGY_FILE.exists():
        return Response("inicio,fin,muestras,porcentaje_baja_tension,caidas,temperatura_max_c,volts_min\n",
                        mimetype="text/csv")
    name = f"energia_{config.slug(config.load()['nombre'])}_{time.strftime('%Y%m%d_%H%M')}.csv"
    return send_file(paths.ENERGY_FILE, mimetype="text/csv", as_attachment=True,
                     download_name=name, max_age=0)


@app.get("/diagnostico.txt")
def diagnostico():
    cfg = config.load()
    seguro = json.loads(json.dumps(cfg))
    for k in ("telegram_token",):
        if seguro["avisos"].get(k):
            seguro["avisos"][k] = seguro["avisos"][k][:6] + "…(oculto)"
    for k in ("ap_clave",):
        seguro["wifi"][k] = "(oculta)"
    seguro["acceso"]["clave"] = "(oculta)" if seguro["acceso"]["clave"] else ""
    out = [f"DIAGNÓSTICO ANTCAM {__version__} - {cfg['nombre']} - {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]

    def sec(t, body):
        out.extend(["=" * 70, t, "=" * 70, body.rstrip(), ""])

    sec("Estado del grabador", json.dumps(recorder_status(), indent=1, ensure_ascii=False))
    sec("Red (AntCam)", json.dumps(net.status(), ensure_ascii=False))
    sec("Configuración", json.dumps(seguro, indent=1, ensure_ascii=False))
    sec("Eventos (últimos 150)", "\n".join(
        f"{time.strftime('%d/%m %H:%M:%S', time.localtime(e['t']))} [{e['nivel']}] {e['msg']}"
        for e in reversed(events.tail(150, "debug"))))
    try:
        sec("Alimentación (energia.csv, últimas 100 líneas)",
            "\n".join(paths.ENERGY_FILE.read_text().splitlines()[-100:]))
    except OSError:
        sec("Alimentación (energia.csv)", "(todavía sin datos)")
    for title, cmd in DIAG_CMDS:
        if paths.SIM:
            sec(title, "(simulación)")
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            sec(title, (r.stdout + r.stderr) or "(sin salida)")
        except Exception as e:
            sec(title, f"(no se pudo ejecutar: {e})")
    name = f"diagnostico_{config.slug(cfg['nombre'])}_{time.strftime('%Y%m%d_%H%M')}.txt"
    return Response("\n".join(out), mimetype="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ------------------------------------------------------------------ sistema
@app.post("/api/sistema/<accion>")
def api_system(accion):
    if accion not in ("reiniciar", "apagar"):
        return fail("acción desconocida", 404)
    events.log("info", "Reinicio pedido desde la página" if accion == "reiniciar"
               else "Apagado pedido desde la página", SRC)

    def go():
        time.sleep(2)
        run(["systemctl", "stop", "antcam-grabador"], timeout=60)  # cierra bien el fragmento
        os.sync()
        run(["systemctl", "reboot" if accion == "reiniciar" else "poweroff"])
    threading.Thread(target=go, daemon=True).start()
    return ok()


def main():
    paths.ensure_dirs()
    threading.Thread(target=net.loop, daemon=True, name="wifi").start()
    threading.Thread(target=notifier.loop, args=(net,), daemon=True, name="avisos").start()
    port = int(os.environ.get("ANTCAM_PORT", "80"))
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
