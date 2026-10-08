"""Rutas del sistema. En modo simulación (ANTCAM_SIM=1) todo vive en ./sim."""

import os
from pathlib import Path

SIM = os.environ.get("ANTCAM_SIM") == "1"

if SIM:
    _base = Path(os.environ.get("ANTCAM_SIM_DIR", "sim")).resolve()
    STATE_DIR = _base / "state"      # configuración y registro de eventos (persistente)
    RUN_DIR = _base / "run"          # estado en vivo, comandos, vista previa (volátil)
    MOUNT_BASE = _base / "drives"    # "pendrives" simulados (una carpeta por pendrive)
else:
    STATE_DIR = Path("/var/lib/antcam")
    RUN_DIR = Path("/run/antcam")
    MOUNT_BASE = Path("/media/antcam")

CONFIG_FILE = STATE_DIR / "config.json"
EVENTS_FILE = STATE_DIR / "eventos.jsonl"
ENERGY_FILE = STATE_DIR / "energia.csv"     # registro de baja tensión cada N minutos
NOTIFIER_STATE = STATE_DIR / "avisos_estado.json"
STATUS_FILE = RUN_DIR / "estado.json"
PREVIEW_FILE = RUN_DIR / "vista.jpg"
PREVIEW_REQ = RUN_DIR / "vista.pedido"
CMD_DIR = RUN_DIR / "cmd"
TIME_SYNC_FLAG = RUN_DIR / "hora_sincronizada"


def ensure_dirs():
    for d in (STATE_DIR, RUN_DIR, CMD_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if SIM:
        MOUNT_BASE.mkdir(parents=True, exist_ok=True)
