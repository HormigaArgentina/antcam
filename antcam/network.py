"""WiFi automático con NetworkManager.

- Si ve una red conocida (oficina, casa, el hotspot de tu celular) se conecta como cliente.
- Si no hay ninguna, crea su propia red ("AntCam-01") para entrar con el celular.
- Estando en red propia y sin nadie conectado, cada N minutos vuelve a buscar redes conocidas.
La grabación no depende de nada de esto.
"""

import re
import socket
import subprocess
import threading
import time

from . import config, events, paths

AP_CON = "antcam-ap"
AP_IP = "10.42.0.1"
IFACE = "wlan0"
SRC = "wifi"


def _nm(*args, timeout=30):
    r = subprocess.run(["nmcli"] + list(args), capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _split(line):
    """nmcli -t separa con ':' y escapa los ':' internos como '\\:'."""
    return [p.replace("\\:", ":") for p in re.split(r"(?<!\\):", line)]


def internet_ok(host="api.telegram.org", timeout=4):
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return True
    except OSError:
        return False


class Network:
    def __init__(self):
        self.mode = "desconocido"   # cliente | propia | sin_red
        self.ssid = None
        self.ip = None
        self.clients = 0
        self.busy = None            # texto mientras busca redes
        self.internet = False
        self.scan = []
        self.ultimo_intento = None  # resultado del último botón Conectar
        self._ap_since = 0
        self._lost_since = None
        self._hold_ap_until = 0
        self._lock = threading.Lock()
        self._sim_known = [{"nombre": "Oficina-SIM", "ssid": "Oficina-SIM"}]

    # ------------------------------------------------------------ lectura de estado
    def update(self):
        if paths.SIM:
            self.mode, self.ssid, self.ip = "cliente", "Oficina-SIM", "127.0.0.1"
            self.internet = True
            return
        try:
            _, out, _ = _nm("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device")
        except Exception:
            self.mode = "desconocido"
            return
        state, con = "", ""
        for line in out.splitlines():
            p = _split(line)
            if len(p) >= 4 and p[0] == IFACE:
                state, con = p[2], p[3]
        if state.startswith("connected") and con == AP_CON:
            self.mode = "propia"
            self.ssid = config.load()["nombre"]
            self.ip = AP_IP
            self.clients = self._count_clients()
        elif state.startswith("connected"):
            self.mode = "cliente"
            self.ssid = self._con_ssid(con) or con
            self.ip = self._ip()
            self.clients = 0
        else:
            self.mode, self.ssid, self.ip, self.clients = "sin_red", None, None, 0

    def _count_clients(self):
        try:
            r = subprocess.run(["iw", "dev", IFACE, "station", "dump"], capture_output=True,
                               text=True, timeout=5)
            return r.stdout.count("Station ")
        except Exception:
            return 0

    def _ip(self):
        try:
            _, out, _ = _nm("-g", "IP4.ADDRESS", "device", "show", IFACE)
            return out.split("/")[0] if out else None
        except Exception:
            return None

    def _con_ssid(self, name):
        try:
            _, out, _ = _nm("-g", "802-11-wireless.ssid", "connection", "show", name)
            return out or None
        except Exception:
            return None

    # ------------------------------------------------------------ redes conocidas
    def known(self):
        if paths.SIM:
            return list(self._sim_known)
        out = []
        try:
            _, lines, _ = _nm("-t", "-f", "NAME,TYPE", "connection", "show")
        except Exception:
            return out
        for line in lines.splitlines():
            p = _split(line)
            if len(p) < 2 or p[1] != "802-11-wireless" or p[0] == AP_CON:
                continue
            _, info, _ = _nm("-g", "802-11-wireless.ssid,802-11-wireless.mode", "connection", "show", p[0])
            parts = info.splitlines()
            if len(parts) >= 2 and parts[1] == "ap":
                continue
            out.append({"nombre": p[0], "ssid": parts[0] if parts else p[0]})
        return out

    def add(self, ssid, clave):
        ssid = ssid.strip()
        if not ssid:
            raise ValueError("Falta el nombre de la red")
        if clave and not 8 <= len(clave) <= 63:
            raise ValueError("La clave WiFi debe tener entre 8 y 63 caracteres")
        if paths.SIM:
            self._sim_known = [k for k in self._sim_known if k["ssid"] != ssid]
            self._sim_known.append({"nombre": ssid, "ssid": ssid})
            return
        for k in self.known():
            if k["ssid"] == ssid:
                _nm("connection", "delete", k["nombre"])
        args = ["connection", "add", "type", "wifi", "ifname", IFACE, "con-name", ssid,
                "ssid", ssid, "connection.autoconnect", "yes",
                "connection.autoconnect-priority", "10"]
        if clave:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", clave]
        code, _, err = _nm(*args)
        if code != 0:
            raise RuntimeError(err or "nmcli falló")
        events.log("info", f"Red WiFi guardada: {ssid}", SRC)

    def delete(self, nombre):
        if paths.SIM:
            self._sim_known = [k for k in self._sim_known if k["nombre"] != nombre]
            return
        _nm("connection", "delete", nombre)
        events.log("info", f"Red WiFi borrada: {nombre}", SRC)

    def scan_list(self):
        if paths.SIM:
            return [{"ssid": "Oficina-SIM", "senal": 70}, {"ssid": "Vecino", "senal": 30}]
        if self.mode == "propia":
            return self.scan  # mientras es red propia no puede buscar
        try:
            _, out, _ = _nm("-t", "-f", "SSID,SIGNAL", "device", "wifi", "list", "--rescan", "auto",
                            timeout=20)
        except Exception:
            return self.scan
        seen = {}
        for line in out.splitlines():
            p = _split(line)
            if len(p) >= 2 and p[0]:
                try:
                    s = int(p[1])
                except ValueError:
                    s = 0
                seen[p[0]] = max(s, seen.get(p[0], 0))
        self.scan = [{"ssid": k, "senal": v} for k, v in sorted(seen.items(), key=lambda x: -x[1])]
        return self.scan

    # ------------------------------------------------------------ red propia
    def ensure_ap_profile(self):
        """Perfil de la red propia. Por defecto es ABIERTA (sin clave): en la Pi 3B+ el chip WiFi
        no completa la conexión WPA2 en modo red propia (probado en el equipo: la red se ve,
        el celular/Mac intentan y nunca entran). Con clave se puede activar si en otra placa anda."""
        if paths.SIM:
            return
        cfg = config.load()
        ssid, clave = cfg["nombre"], cfg["wifi"]["ap_clave"]
        abierta = cfg["wifi"].get("ap_abierta", True)
        seguridad = [] if abierta else [
            "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", clave,
            "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp",
            "wifi-sec.pmf", "disable"]
        code, actual, _ = _nm("-g", "802-11-wireless-security.key-mgmt", "connection", "show", AP_CON)
        if code == 0:
            tiene_clave = bool(actual.strip())
            if tiene_clave == (not abierta):
                args = ["802-11-wireless.ssid", ssid, "802-11-wireless.powersave", "2"]
                if not abierta:
                    args += ["wifi-sec.psk", clave]
                _nm("connection", "modify", AP_CON, *args)
                return
            _nm("connection", "delete", AP_CON)  # cambió abierta <-> con clave: se rehace
        _nm("connection", "add", "type", "wifi", "ifname", IFACE, "con-name", AP_CON,
            "autoconnect", "no", "ssid", ssid,
            "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg", "802-11-wireless.channel", "6",
            "802-11-wireless.powersave", "2",
            "ipv4.method", "shared", "ipv4.addresses", f"{AP_IP}/24", "ipv6.method", "disabled",
            *seguridad)

    def start_ap(self, hold_min=0):
        if hold_min:
            self._hold_ap_until = time.time() + hold_min * 60
        if paths.SIM:
            return True
        with self._lock:
            self.ensure_ap_profile()
            code, _, err = _nm("connection", "up", AP_CON, timeout=45)
            if code == 0:
                self._ap_since = time.time()
                events.log("info", f"Red propia activa: {config.load()['nombre']} "
                                   f"(entrar a http://{AP_IP})", SRC)
                return True
            events.log("error", f"No se pudo crear la red propia: {err}", SRC)
            return False

    def connect_to(self, nombre):
        """Conectarse a una red conocida elegida por el usuario (botón Conectar).

        Si no puede (fuera de alcance o clave mal), vuelve a crear la red propia
        y la mantiene 10 min para que el usuario pueda volver a entrar.
        """
        k = next((k for k in self.known() if k["nombre"] == nombre), None)
        if not k:
            return False
        ssid = k["ssid"]
        if paths.SIM:
            self.ultimo_intento = {"ssid": ssid, "ok": True, "t": time.time()}
            return True
        with self._lock:
            self.busy = f"Conectando a {ssid}…"
            ok = False
            try:
                if self.mode == "propia":
                    _nm("connection", "down", AP_CON)
                    time.sleep(3)
                for intento in range(2):
                    code, _, err = _nm("connection", "up", nombre, timeout=45)
                    if code == 0:
                        ok = True
                        break
                    _nm("device", "wifi", "rescan", timeout=15)  # a veces la red no estaba en la lista
                    time.sleep(5)
            finally:
                self.busy = None
                self.update()
        self.ultimo_intento = {"ssid": ssid, "ok": ok, "t": time.time()}
        if ok:
            events.log("info", f"Conectado a la red {ssid} (elegida desde la página)", SRC)
            return True
        events.log("aviso", f"No se pudo conectar a {ssid}: ¿está al alcance y la clave es correcta? "
                            "Se volvió a crear la red propia.", SRC)
        self._ap_since = 0
        self.start_ap(hold_min=10)
        return False

    def try_known(self):
        """Apaga la red propia un momento y prueba conectarse a una red conocida."""
        if paths.SIM:
            return True
        with self._lock:
            known = self.known()
            if not known:
                return False
            self.busy = "Buscando redes conocidas…"
            try:
                if self.mode == "propia":
                    _nm("connection", "down", AP_CON)
                    time.sleep(3)
                visible = {s["ssid"] for s in self._fresh_scan()}
                for k in known:
                    if k["ssid"] in visible:
                        code, _, _ = _nm("connection", "up", k["nombre"], timeout=45)
                        if code == 0:
                            events.log("info", f"Conectado a la red {k['ssid']}", SRC)
                            return True
                return False
            finally:
                self.busy = None
                self.update()
                if self.mode != "cliente":
                    self._ap_since = 0

    def _fresh_scan(self):
        try:
            _nm("device", "wifi", "rescan", timeout=15)
        except Exception:
            pass
        time.sleep(4)
        self.mode = "sin_red"
        return self.scan_list()

    # ------------------------------------------------------------ bucle
    def loop(self):
        if not paths.SIM:
            try:
                self.ensure_ap_profile()
            except Exception as e:
                events.log("error", f"No se pudo preparar la red propia: {e}", SRC)
        boot = time.time()
        last_inet = 0
        while True:
            try:
                self.update()
                now = time.time()
                cfg = config.load()
                if self.mode == "cliente":
                    self._lost_since = None
                    if now - last_inet > 60:
                        self.internet = internet_ok()
                        last_inet = now
                elif self.mode == "propia":
                    self.internet = False
                    if not self._ap_since:
                        self._ap_since = now
                    wait = cfg["wifi"]["reintento_min"] * 60
                    if (self.clients == 0 and now > self._hold_ap_until
                            and now - self._ap_since > wait and self.known()):
                        if not self.try_known():
                            self.start_ap()
                else:  # sin red: esperar a que se conecte sola; si no, red propia
                    self.internet = False
                    if self._lost_since is None:
                        self._lost_since = now
                    grace = 90 if now - boot < 300 else 45
                    if now - self._lost_since > grace and not self.busy:
                        self.start_ap()
                        self._lost_since = None
            except Exception as e:
                print("wifi:", e, flush=True)
            time.sleep(10)

    def status(self):
        addr = {"cliente": f"http://{config.slug(config.load()['nombre'])}.local",
                "propia": f"http://{AP_IP}"}.get(self.mode)
        return {"modo": self.mode, "ssid": self.ssid, "ip": self.ip, "clientes": self.clients,
                "buscando": self.busy, "internet": self.internet, "direccion": addr,
                "ultimo_intento": self.ultimo_intento}
