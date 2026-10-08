"""Avisos por Telegram.

Cuando hay internet manda un resumen cada N horas (con foto opcional) y los problemas
apenas ocurren. Sin internet guarda todo y, al volver la conexión, manda lo que pasó.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

from . import config, events, paths

SRC = "avisos"
API = "https://api.telegram.org/bot{token}/{method}"


def _fmt_t(t):
    return datetime.fromtimestamp(t).strftime("%d/%m %H:%M")


def _dur(s):
    s = int(s or 0)
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d} d {h} h" if d else (f"{h} h {m} min" if h else f"{m} min")


def tg(token, method, params=None, photo=None, timeout=20):
    url = API.format(token=token, method=method)
    params = {k: str(v) for k, v in (params or {}).items()}
    if photo:
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in params.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                     f"{v}\r\n").encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
                 f"filename=\"vista.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n").encode()
        body += photo + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError):
        return {"ok": False, "description": "No hay conexión con Telegram (¿el equipo tiene internet?)"}


def detect_chat(token):
    """Después de mandarle cualquier mensaje al bot, devuelve (chat_id, nombre)."""
    r = tg(token, "getUpdates", {"limit": 20})
    if not r.get("ok"):
        raise RuntimeError(r.get("description", "token inválido"))
    for upd in reversed(r.get("result", [])):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            name = chat.get("title") or chat.get("first_name") or chat.get("username") or ""
            return str(chat["id"]), name
    raise RuntimeError("No encontré mensajes. Mandale un mensaje cualquiera al bot y volvé a probar.")


def read_status():
    try:
        with open(paths.STATUS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def get_photo(timeout=5):
    t0 = time.time()
    paths.PREVIEW_REQ.touch()
    while time.time() - t0 < timeout:
        try:
            if paths.PREVIEW_FILE.stat().st_mtime > t0:
                return paths.PREVIEW_FILE.read_bytes()
        except OSError:
            pass
        time.sleep(0.3)
    return None


ICON = {"grabando": "✅", "pausado": "⏸️", "encuadre": "🎯", "prueba": "🧪",
        "error": "❌", "sin_espacio": "🟥", "iniciando": "⏳"}
NAME = {"grabando": "grabando", "pausado": "en pausa", "encuadre": "en modo encuadre",
        "prueba": "haciendo prueba de calidad", "error": "con un problema",
        "sin_espacio": "SIN ESPACIO para grabar", "iniciando": "iniciando"}


def status_text(nombre, st):
    if not st or time.time() - st.get("actualizado", 0) > 30:
        return f"❌ {nombre}: el servicio de grabación no responde"
    e = st["estado"]
    lines = [f"{ICON.get(e, '•')} {nombre} {NAME.get(e, e)}"]
    if st.get("mensaje") and e != "grabando":
        lines.append(st["mensaje"])
    v = st.get("video") or {}
    if v.get("tamano"):
        fps = v.get("fps_actual") or v.get("fps_config")
        lines.append(f"🎥 {v['tamano'][0]}×{v['tamano'][1]} · {fps} cuadros/s · {v['bitrate_mbps']} Mbps")
    a = st.get("almacenamiento") or {}
    for p in a.get("pendrives", []):
        usado = 100 - (100 * p["libre_gb"] / p["total_gb"]) if p["total_gb"] else 0
        marca = " ◀ grabando acá" if p["actual"] else ""
        lines.append(f"💾 {p['nombre']}: {usado:.0f}% usado, {p['libre_gb']} GB libres ({p['estado']}){marca}")
    if not a.get("pendrives"):
        lines.append("💾 No hay pendrives conectados")
    elif a.get("dias_restantes") is not None:
        lines.append(f"⏳ Espacio para ~{a['dias_restantes']} días más")
    s = st.get("sistema") or {}
    extra = []
    if s.get("temperatura"):
        extra.append(f"CPU {s['temperatura']:.0f} °C")
    if s.get("uptime_s"):
        extra.append(f"encendido hace {_dur(s['uptime_s'])}")
    if s.get("hubo_baja_tension"):
        extra.append("⚠️ hubo baja tensión")
    if extra:
        lines.append("🌡️ " + " · ".join(extra))
    return "\n".join(lines)


class Notifier:
    def __init__(self):
        self.st = self._load()
        self.last_error = None

    def _load(self):
        try:
            with open(paths.NOTIFIER_STATE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"offset": None, "inode": None, "ultimo_resumen": 0, "ultimo_ok": 0,
                    "pendientes": [], "fragmentos": 0, "incompletos": 0}

    def _save(self):
        try:
            with open(paths.NOTIFIER_STATE, "w") as f:
                json.dump(self.st, f, ensure_ascii=False)
        except OSError:
            pass

    def _collect(self):
        if self.st["offset"] is None:  # primera vez: no mandar la historia vieja
            try:
                self.st["offset"] = paths.EVENTS_FILE.stat().st_size
                self.st["inode"] = paths.EVENTS_FILE.stat().st_ino
            except OSError:
                self.st["offset"] = 0
            return
        evs, self.st["offset"], self.st["inode"] = events.read_from(self.st["offset"], self.st["inode"])
        for e in evs:
            if e.get("tipo") == "fragmento":
                self.st["fragmentos"] += 1
                self.st["incompletos"] += 1 if e.get("incompleto") else 0
            elif e.get("nivel") in ("aviso", "error") and e.get("origen") != SRC:
                self.st["pendientes"].append([e["t"], e["nivel"], e["msg"]])
        self.st["pendientes"] = self.st["pendientes"][-25:]

    def _compose(self, cfg, periodic):
        nombre = cfg["nombre"]
        parts = []
        gap = time.time() - self.st["ultimo_ok"] if self.st["ultimo_ok"] else 0
        if gap > max(2, cfg["avisos"]["intervalo_h"] * 1.5) * 3600:
            parts.append(f"📶 Volvió la conexión (último aviso: {_fmt_t(self.st['ultimo_ok'])})")
        if self.st["pendientes"]:
            parts.append("⚠️ Novedades:\n" + "\n".join(
                f"• {_fmt_t(t)} {'❌' if lv == 'error' else '⚠️'} {msg}"
                for t, lv, msg in self.st["pendientes"]))
        if periodic or self.st["pendientes"]:
            parts.append(status_text(nombre, read_status()))
        if periodic and self.st["ultimo_resumen"]:
            inc = f", {self.st['incompletos']} incompletos" if self.st["incompletos"] else ""
            parts.append(f"🗂️ Desde {_fmt_t(self.st['ultimo_resumen'])}: "
                         f"{self.st['fragmentos']} fragmentos grabados{inc}")
        return "\n\n".join(parts)

    def send(self, cfg, text, photo=None):
        token, chat = cfg["avisos"]["telegram_token"], cfg["avisos"]["telegram_chat_id"]
        try:
            if photo and len(text) <= 1000:
                r = tg(token, "sendPhoto", {"chat_id": chat, "caption": text}, photo=photo)
            else:
                r = tg(token, "sendMessage", {"chat_id": chat, "text": text})
                if r.get("ok") and photo:
                    tg(token, "sendPhoto", {"chat_id": chat}, photo=photo)
        except Exception as e:
            r = {"ok": False, "description": str(e)}
        if r.get("ok"):
            self.last_error = None
            self.st["ultimo_ok"] = time.time()
            return True
        self.last_error = r.get("description", "error desconocido")
        return False

    def tick(self, online):
        cfg = config.load()
        self._collect()
        av = cfg["avisos"]
        if not (av["telegram_token"] and av["telegram_chat_id"]):
            self.st["pendientes"] = []
            self._save()
            return
        periodic = time.time() - self.st["ultimo_resumen"] >= av["intervalo_h"] * 3600
        if (periodic or self.st["pendientes"]) and online:
            photo = get_photo() if periodic and av["foto"] else None
            if self.send(cfg, self._compose(cfg, periodic), photo):
                self.st["pendientes"] = []
                if periodic:
                    self.st["ultimo_resumen"] = time.time()
                    self.st["fragmentos"] = self.st["incompletos"] = 0
        self._save()

    def test(self):
        cfg = config.load()
        text = "👋 Prueba de avisos\n\n" + status_text(cfg["nombre"], read_status())
        ok = self.send(cfg, text, get_photo() if cfg["avisos"]["foto"] else None)
        self._save()
        return ok

    def info(self):
        return {"ultimo_ok": self.st.get("ultimo_ok"), "ultimo_resumen": self.st.get("ultimo_resumen"),
                "pendientes": len(self.st.get("pendientes", [])), "error": self.last_error}

    def loop(self, network):
        time.sleep(20)
        while True:
            try:
                self.tick(network.internet)
            except Exception as e:
                print("avisos:", e, flush=True)
            time.sleep(30)
