"""Comandos por Telegram: el bot responde a /estado, /foto, /red, etc.

Corre dentro del servicio web, en su propio hilo, solo cuando hay internet y el bot está
vinculado. Responde únicamente al chat vinculado (persona o grupo); a cualquier otro chat le
contesta que no está autorizado y lo recuerda como candidato para el botón "Vincular".

También avisa cuando el equipo se conecta a una red nueva, con la dirección para entrar.
"""

import os
import subprocess
import time
from datetime import datetime

from . import config, events, paths
from .notifier import get_photo, read_status, status_text, tg

SRC = "bot"
ESPERA_S = 25  # long polling de getUpdates

COMANDOS = [
    ("estado", "Cómo está el equipo: grabación, pendrives, temperatura"),
    ("foto", "Foto actual de la cámara"),
    ("red", "A qué red está conectado y su dirección"),
    ("tension", "Alimentación en los últimos 30 minutos"),
    ("eventos", "Últimos eventos del registro"),
    ("grabar", "Reanudar la grabación"),
    ("pausar", "Detener la grabación"),
    ("reiniciar", "Reiniciar el equipo (pide confirmación)"),
    ("ayuda", "Lista de comandos"),
]


def _cmd_file(name, arg=""):
    paths.CMD_DIR.mkdir(parents=True, exist_ok=True)
    (paths.CMD_DIR / f"{name}.{time.time_ns()}").write_text(str(arg))


def texto_red(nombre, net):
    s = net.status()
    if s["modo"] == "cliente":
        ip = s.get("ip") or "?"
        return (f"📶 {nombre} conectada a la red {s.get('ssid') or '?'}"
                f"{' (con internet)' if s.get('internet') else ''}\n"
                f"Dirección: http://{ip}  ·  {s.get('direccion') or ''}\n"
                "Para entrar, tu celular o PC tiene que estar en esa misma red.")
    if s["modo"] == "propia":
        return f"📡 {nombre} en su red propia ({nombre}, abierta). Dirección: http://10.42.0.1"
    return f"❔ {nombre} sin red WiFi en este momento"


def texto_tension(st):
    e = (st or {}).get("energia") or {}
    if not e:
        return "🔌 Todavía no hay datos de alimentación (¿versión del grabador anterior?)"
    ahora = e.get("ahora_baja")
    lineas = ["🔌 Alimentación: " + ("⚠️ BAJA TENSIÓN AHORA" if ahora else "✅ bien ahora"
                                    if ahora is False else "sin dato")]
    if e.get("pct_30min") is not None:
        lineas.append(f"Últimos 30 min: baja tensión el {e['pct_30min']}% del tiempo, "
                      f"{e.get('caidas_30min', 0)} caída(s)")
    if e.get("volts") is not None:
        lineas.append(f"Entrada: {e['volts']:.2f} V")
    tramos = e.get("tramos") or []
    if tramos:  # mini gráfico: 30 bloques de 1 minuto
        bloques = []
        for i in range(0, len(tramos), 6):
            grupo = tramos[i:i + 6]
            frac = max(t[1] for t in grupo)
            bloques.append("🟩" if frac == 0 else "🟥" if frac > 0.3 else "🟧")
        lineas.append("".join(bloques[-30:]) + "  (cada cuadrito = 1 min, el último es ahora)")
    return "\n".join(lineas)


def texto_eventos(n=12):
    evs = events.tail(n, "info")
    if not evs:
        return "🗒️ El registro está vacío"
    icon = {"error": "❌", "aviso": "⚠️"}
    return "🗒️ Últimos eventos:\n" + "\n".join(
        f"{datetime.fromtimestamp(e['t']).strftime('%d/%m %H:%M')} {icon.get(e['nivel'], '•')} {e['msg']}"
        for e in reversed(evs))


class Bot:
    def __init__(self, notifier, net):
        self.notifier = notifier
        self.net = net
        self.offset = None
        self.token_menu = None    # token con el que ya se cargó el menú de comandos
        self.red_avisada = None   # (ssid, ip) de la última red avisada
        self.confirmar_reinicio = 0

    # ---------------------------------------------------------------- envío
    def _responder(self, token, chat, texto, foto=None):
        if foto:
            r = tg(token, "sendPhoto", {"chat_id": chat, "caption": texto[:1000]}, photo=foto)
            if r.get("ok"):
                return
        tg(token, "sendMessage", {"chat_id": chat, "text": texto})

    # ---------------------------------------------------------------- comandos
    def _atender(self, cfg, texto):
        nombre = cfg["nombre"]
        partes = texto.strip().split()
        cmd = partes[0].lstrip("/").split("@")[0].lower() if partes else ""
        arg = partes[1].lower() if len(partes) > 1 else ""
        st = read_status()
        if cmd in ("start", "ayuda", "help"):
            return ("🐜 Comandos de " + nombre + ":\n" +
                    "\n".join(f"/{c} — {d}" for c, d in COMANDOS)), None
        if cmd == "estado":
            return status_text(nombre, st) + "\n\n" + texto_red(nombre, self.net), None
        if cmd == "foto":
            foto = get_photo(timeout=8)
            if not foto:
                return "📷 No pude sacar la foto: la cámara no está entregando imagen ahora.", None
            return f"📷 {nombre} · {datetime.now().strftime('%d/%m %H:%M:%S')}", foto
        if cmd == "red":
            return texto_red(nombre, self.net), None
        if cmd in ("tension", "tensión", "energia"):
            return texto_tension(st), None
        if cmd == "eventos":
            return texto_eventos(), None
        if cmd in ("grabar", "pausar"):
            on = cmd == "grabar"
            config.update({"grabacion": {"habilitada": on}})
            _cmd_file("recargar")
            events.log("info", "Grabación " + ("reanudada" if on else "detenida") + " desde Telegram", SRC)
            return ("▶️ Grabación reanudada." if on else
                    "⏸️ Grabación detenida. No graba hasta que mandes /grabar (tampoco al reiniciar)."), None
        if cmd == "reiniciar":
            if arg in ("si", "sí", "confirmar") and time.time() - self.confirmar_reinicio < 120:
                events.log("info", "Reinicio pedido desde Telegram", SRC)
                self._reiniciar()
                return "🔄 Reiniciando… vuelve en unos 2 minutos.", None
            self.confirmar_reinicio = time.time()
            return "¿Seguro? Mandá  /reiniciar si  en los próximos 2 minutos para confirmar.", None
        return "No conozco ese comando. Mandá /ayuda para ver la lista.", None

    def _reiniciar(self):
        if paths.SIM:
            print("SIM: reinicio", flush=True)
            return

        def go():
            time.sleep(3)
            subprocess.run(["systemctl", "stop", "antcam-grabador"], timeout=60)
            os.sync()
            subprocess.run(["systemctl", "reboot"])
        import threading
        threading.Thread(target=go, daemon=True).start()

    # ---------------------------------------------------------------- ciclo
    def _menu(self, token):
        if self.token_menu == token:
            return
        r = tg(token, "setMyCommands", {"commands": __import__("json").dumps(
            [{"command": c, "description": d} for c, d in COMANDOS], ensure_ascii=False)})
        if r.get("ok"):
            self.token_menu = token

    def _aviso_red(self, cfg, token, chat):
        s = self.net.status()
        if s["modo"] != "cliente" or not s.get("internet"):
            return
        clave = (s.get("ssid"), s.get("ip"))
        if clave == self.red_avisada:
            return
        r = tg(token, "sendMessage", {"chat_id": chat, "text": texto_red(cfg["nombre"], self.net)})
        if r.get("ok"):
            self.red_avisada = clave

    def paso(self):
        cfg = config.load()
        av = cfg["avisos"]
        token, chat = av["telegram_token"], av["telegram_chat_id"]
        if not token or not (self.net.internet or paths.SIM):
            time.sleep(10)
            return
        if not chat:  # sin vincular: no consumir mensajes, que los use el botón Vincular
            time.sleep(10)
            return
        self._menu(token)
        self._aviso_red(cfg, token, chat)
        params = {"timeout": ESPERA_S, "allowed_updates": '["message","channel_post"]'}
        if self.offset is not None:
            params["offset"] = self.offset
        r = tg(token, "getUpdates", params, timeout=ESPERA_S + 10)
        if not r.get("ok"):
            time.sleep(15)
            return
        for upd in r.get("result", []):
            self.offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("channel_post") or {}
            texto = msg.get("text") or ""
            de = str((msg.get("chat") or {}).get("id", ""))
            if not texto.startswith("/") or not de:
                continue
            if de != str(chat):
                self.notifier.st["chat_candidato"] = de
                tg(token, "sendMessage", {"chat_id": de, "text":
                   "🔒 Este chat no está vinculado a la AntCam. Para usarlo, tocá «Vincular» "
                   "en la página del equipo (Conexión → Avisos por Telegram)."})
                continue
            try:
                respuesta, foto = self._atender(cfg, texto)
            except Exception as e:
                respuesta, foto = f"❌ Error: {e}", None
            self._responder(token, chat, respuesta, foto)

    def loop(self):
        time.sleep(15)
        while True:
            try:
                self.paso()
            except Exception as e:
                print("bot:", e, flush=True)
                time.sleep(15)
