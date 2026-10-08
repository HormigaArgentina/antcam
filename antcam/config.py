"""Configuración persistente (JSON). Solo el servicio web la escribe; el grabador la lee."""

import copy
import json
import os
import re

from . import paths

DEFAULTS = {
    "nombre": "AntCam-01",
    "grabacion": {
        "habilitada": True,        # grabar al encender
        "ancho": 1280,             # ancho del video de salida (el alto sale de la zona elegida)
        "fps": 30,
        "bitrate_mbps": 3.0,
        "segmento_min": 30,        # largo de cada fragmento
        "alinear_reloj": True,     # cortar fragmentos en horas "redondas" (08:00, 08:30...)
        "fijar_exposicion": False, # congela exposición y balance de blancos tras 10 s
        "rotar_180": False,
        "zona": [0.0, 0.0, 1.0, 1.0],  # x, y, ancho, alto (fracciones del cuadro completo)
    },
    "rotulo": {                    # marca en el video, abajo a la izquierda
        "fecha_hora": True,
        "lugar": "",
        "especie": "",
        "nota": "",
    },
    "almacenamiento": {
        "reserva_gb": 1.0,         # espacio que se deja libre en cada pendrive
    },
    "wifi": {
        "ap_clave": "hormigas2026",
        "reintento_min": 15,       # cada cuánto busca redes conocidas estando en modo propio
    },
    "avisos": {
        "telegram_token": "",
        "telegram_chat_id": "",
        "intervalo_h": 6,
        "foto": True,
    },
    "acceso": {
        "clave": "",               # si se completa, la página pide usuario "antcam" y esta clave
    },
    "leds_externos": True,         # LEDs del equipo anterior: verde GPIO22, amarillo GPIO27, rojo GPIO14
}

ANCHOS = [640, 960, 1280, 1600, 1920]
FPS = [10, 15, 20, 25, 30]
SEGMENTOS = [5, 10, 15, 30, 60]


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    try:
        with open(paths.CONFIG_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    return validate(_merge(DEFAULTS, data))


def save(cfg):
    cfg = validate(cfg)
    paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = paths.CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, paths.CONFIG_FILE)
    return cfg


def update(partial):
    return save(_merge(load(), partial))


def _clamp(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _closest(v, options, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return min(options, key=lambda o: abs(o - v))


def slug(nombre):
    s = re.sub(r"[^a-z0-9-]+", "-", nombre.lower()).strip("-")
    return s[:30] or "antcam"


def validate(cfg):
    d = DEFAULTS
    nombre = str(cfg.get("nombre") or d["nombre"]).strip()
    cfg["nombre"] = re.sub(r"[^A-Za-z0-9_-]+", "-", nombre)[:30] or d["nombre"]

    g = cfg["grabacion"]
    g["habilitada"] = bool(g.get("habilitada"))
    g["ancho"] = int(_closest(g.get("ancho"), ANCHOS, d["grabacion"]["ancho"]))
    g["fps"] = int(_closest(g.get("fps"), FPS, d["grabacion"]["fps"]))
    g["bitrate_mbps"] = round(_clamp(g.get("bitrate_mbps"), 0.5, 15, 3.0), 1)
    g["segmento_min"] = int(_closest(g.get("segmento_min"), SEGMENTOS, 30))
    g["alinear_reloj"] = bool(g.get("alinear_reloj"))
    g["fijar_exposicion"] = bool(g.get("fijar_exposicion"))
    g["rotar_180"] = bool(g.get("rotar_180"))
    try:
        x, y, w, h = [float(v) for v in g.get("zona")]
    except (TypeError, ValueError):
        x, y, w, h = 0.0, 0.0, 1.0, 1.0
    x = min(max(x, 0.0), 0.95)
    y = min(max(y, 0.0), 0.95)
    w = min(max(w, 0.05), 1.0 - x)
    h = min(max(h, 0.05), 1.0 - y)
    g["zona"] = [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]

    r = cfg["rotulo"]
    r["fecha_hora"] = bool(r.get("fecha_hora"))
    for k in ("lugar", "especie", "nota"):
        r[k] = re.sub(r"[\x00-\x1f]+", " ", str(r.get(k) or "")).strip()[:40]

    a = cfg["almacenamiento"]
    a["reserva_gb"] = round(_clamp(a.get("reserva_gb"), 0.2, 20, 1.0), 1)

    wf = cfg["wifi"]
    clave = str(wf.get("ap_clave") or "")
    wf["ap_clave"] = clave if 8 <= len(clave) <= 63 else d["wifi"]["ap_clave"]
    wf["reintento_min"] = int(_clamp(wf.get("reintento_min"), 5, 240, 15))

    av = cfg["avisos"]
    av["telegram_token"] = str(av.get("telegram_token") or "").strip()
    av["telegram_chat_id"] = str(av.get("telegram_chat_id") or "").strip()
    av["intervalo_h"] = int(_clamp(av.get("intervalo_h"), 1, 48, 6))
    av["foto"] = bool(av.get("foto"))

    cfg["acceso"]["clave"] = str(cfg["acceso"].get("clave") or "")
    cfg["leds_externos"] = bool(cfg.get("leds_externos"))
    return cfg


def output_size(cfg, sensor_w=3280, sensor_h=2464):
    """Tamaño del video de salida: ancho elegido, alto según la forma de la zona.

    Múltiplos de 32x16 (lo que pide el codificador de la Pi), máximo 1920x1080.
    """
    g = cfg["grabacion"]
    _, _, fw, fh = g["zona"]
    aspect = (fw * sensor_w) / (fh * sensor_h)
    w = g["ancho"]
    h = w / aspect
    if h > 1080:
        h = 1080
        w = h * aspect
    w = max(64, int(round(w / 32)) * 32)
    h = max(64, int(round(h / 16)) * 16)
    return min(w, 1920), min(h, 1080)
