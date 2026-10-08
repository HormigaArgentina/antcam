"""Registro de eventos compartido (un JSON por línea).

Niveles: debug (fragmentos), info, aviso, error. Los avisos y errores se mandan por Telegram.
"""

import json
import os
import time

from . import paths

MAX_BYTES = 2_000_000


def log(level, msg, source="", **extra):
    entry = {"t": time.time(), "nivel": level, "msg": msg, "origen": source}
    entry.update(extra)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    try:
        paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
        # O_APPEND con una sola escritura: seguro entre procesos para líneas cortas
        fd = os.open(paths.EVENTS_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except OSError:
        pass
    print(f"[{level}] {source}: {msg}", flush=True)


def rotate_if_big():
    try:
        if paths.EVENTS_FILE.stat().st_size > MAX_BYTES:
            os.replace(paths.EVENTS_FILE, paths.EVENTS_FILE.with_suffix(".jsonl.1"))
    except OSError:
        pass


def read_from(offset, inode):
    """Lee eventos nuevos desde una posición. Devuelve (eventos, nuevo_offset, inode)."""
    try:
        st = paths.EVENTS_FILE.stat()
    except OSError:
        return [], 0, None
    if st.st_ino != inode or st.st_size < offset:
        offset = 0  # el archivo se rotó
    out = []
    with open(paths.EVENTS_FILE, "rb") as f:
        f.seek(offset)
        data = f.read()
    # solo líneas completas
    end = data.rfind(b"\n") + 1
    for raw in data[:end].splitlines():
        try:
            out.append(json.loads(raw))
        except ValueError:
            pass
    return out, offset + end, st.st_ino


def tail(n=100, min_level="info"):
    order = {"debug": 0, "info": 1, "aviso": 2, "error": 3}
    try:
        with open(paths.EVENTS_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for raw in reversed(lines):
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if order.get(e.get("nivel"), 1) >= order[min_level]:
            out.append(e)
            if len(out) >= n:
                break
    return out
