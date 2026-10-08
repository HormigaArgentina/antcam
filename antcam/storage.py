"""Pendrives en cadena.

Detecta los pendrives USB conectados, los monta solo (no hace falta escritorio) y elige en cuál
grabar: sigue en el actual hasta que se llena y pasa al que tenga más espacio libre.
"""

import errno
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import events, paths

SUPPORTED = ("vfat", "exfat", "ntfs", "ext4")
FOLDER = "AntCam"


def _run(cmd, timeout=20):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


class Storage:
    def __init__(self, nombre, reserva_gb):
        self.nombre = nombre
        self.reserve = int(reserva_gb * 1e9)
        self.drives = {}          # id -> dict
        self.current_id = None
        self._lock = threading.Lock()
        self._blocked = {}        # id -> motivo ("lleno", "expulsado", "error") y hora
        self._last_choice_msg = None

    # ------------------------------------------------------------------ detección
    def _scan_real(self):
        try:
            r = _run(["lsblk", "-J", "-b", "-o",
                      "NAME,PATH,FSTYPE,LABEL,UUID,MOUNTPOINT,SIZE,TRAN,TYPE,RO"])
            tree = json.loads(r.stdout or "{}").get("blockdevices", [])
        except Exception as e:
            events.log("error", f"No se pudo listar los dispositivos USB: {e}", "almacenamiento")
            return []
        found = []
        for disk in tree:
            if disk.get("tran") != "usb":
                continue
            parts = disk.get("children") or [disk]
            for p in parts:
                fs = (p.get("fstype") or "").lower()
                if fs not in SUPPORTED:
                    continue
                found.append({
                    "id": p.get("uuid") or p.get("path"),
                    "dev": p.get("path"),
                    "label": p.get("label") or "",
                    "fstype": fs,
                    "mount": p.get("mountpoint"),
                    "ro": str(p.get("ro")) in ("1", "True", "true"),
                })
        return found

    def _mount(self, d):
        name = re.sub(r"[^A-Za-z0-9_-]", "_", d["label"] or d["id"])[:40] or "usb"
        mnt = paths.MOUNT_BASE / name
        mnt.mkdir(parents=True, exist_ok=True)
        fs = d["fstype"]
        tries = []
        if fs in ("vfat", "exfat"):
            tries = [["-t", fs, "-o", "rw,noatime,umask=0000"]]
        elif fs == "ntfs":
            tries = [["-t", "ntfs3", "-o", "rw,noatime,umask=0000"],
                     ["-t", "ntfs-3g", "-o", "rw,noatime,umask=0000"]]
        else:
            tries = [["-t", fs, "-o", "rw,noatime"]]
        err = ""
        for opts in tries:
            r = _run(["mount"] + opts + [d["dev"], str(mnt)])
            if r.returncode == 0:
                events.log("info", f"Pendrive montado: {d['label'] or d['dev']} ({fs})", "almacenamiento")
                return str(mnt)
            err = r.stderr.strip()
        events.log("error", f"No se pudo montar {d['dev']}: {err}", "almacenamiento")
        return None

    def _scan_sim(self):
        out = []
        for p in sorted(paths.MOUNT_BASE.iterdir()) if paths.MOUNT_BASE.exists() else []:
            if p.is_dir():
                out.append({"id": "sim-" + p.name, "dev": str(p), "label": p.name,
                            "fstype": "exfat", "mount": str(p), "ro": False})
        return out

    @staticmethod
    def _sim_space(mount):
        try:
            cap = float((Path(mount) / "capacidad_gb").read_text())
        except (OSError, ValueError):
            cap = 1.0
        used = sum(f.stat().st_size for f in Path(mount).rglob("*") if f.is_file())
        total = int(cap * 1e9)
        return total, max(0, total - used)

    def refresh(self):
        """Re-escanea pendrives (llamar cada ~10 s desde el bucle principal)."""
        found = self._scan_sim() if paths.SIM else self._scan_real()
        seen = set()
        new = {}
        for d in found:
            seen.add(d["id"])
            block = self._blocked.get(d["id"])
            if block and block[0] == "expulsado":
                d.update(total=0, free=0, state="expulsado")
                new[d["id"]] = d
                continue
            if not d["mount"] and not paths.SIM:
                d["mount"] = self._mount(d)
            if not d["mount"]:
                d.update(total=0, free=0, state="error")
                new[d["id"]] = d
                continue
            try:
                if paths.SIM:
                    total, free = self._sim_space(d["mount"])
                else:
                    st = os.statvfs(d["mount"])
                    total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
            except OSError:
                total, free = 0, 0
            d.update(total=total, free=free)
            state = "ok"
            if d["ro"]:
                state = "solo_lectura"
            elif free < self.reserve:
                state = "lleno"
            elif block and block[0] == "error" and time.time() - block[1] < 120:
                state = "error"
            elif block and block[0] == "lleno" and free < self.reserve * 2:
                state = "lleno"
            d["state"] = state
            new[d["id"]] = d

        # pendrives que desaparecieron
        for gone in set(self.drives) - seen:
            old = self.drives[gone]
            self._blocked.pop(gone, None)
            if old.get("state") != "expulsado":
                events.log("aviso", f"Se quitó el pendrive {old['label'] or old['dev']} sin expulsarlo",
                           "almacenamiento")
            if old.get("mount") and not paths.SIM:
                _run(["umount", "-l", old["mount"]])

        with self._lock:
            self.drives = new
            self._choose()

    def _choose(self):
        cur = self.drives.get(self.current_id)
        if cur and cur["state"] == "ok":
            return
        ok = [d for d in self.drives.values() if d["state"] == "ok"]
        prev = self.current_id
        if not ok:
            self.current_id = None
            if prev is not None or self._last_choice_msg != "none":
                events.log("error", "No hay ningún pendrive con espacio: la grabación está detenida",
                           "almacenamiento")
                self._last_choice_msg = "none"
            return
        best = max(ok, key=lambda d: d["free"])
        self.current_id = best["id"]
        self._last_choice_msg = best["id"]
        nombre = best["label"] or best["dev"]
        if prev and prev != best["id"]:
            events.log("aviso", f"Se cambió de pendrive: ahora se graba en {nombre} "
                                f"({best['free'] / 1e9:.1f} GB libres)", "almacenamiento")
        elif prev is None:
            events.log("info", f"Grabando en el pendrive {nombre} ({best['free'] / 1e9:.1f} GB libres)",
                       "almacenamiento")

    # ------------------------------------------------------------------ uso
    def target(self):
        """Dónde grabar el próximo fragmento (lo llama el hilo escritor)."""
        with self._lock:
            d = self.drives.get(self.current_id)
            if not d or d["state"] != "ok":
                return None
            base = Path(d["mount"]) / FOLDER / self.nombre
            return {"dir": base, "id": d["id"], "fstype": d["fstype"]}

    def mark_error(self, drive_id, exc):
        if drive_id is None:
            return
        full = isinstance(exc, OSError) and exc.errno == errno.ENOSPC
        self._blocked[drive_id] = ("lleno" if full else "error", time.time())
        d = self.drives.get(drive_id, {})
        nombre = d.get("label") or d.get("dev") or drive_id
        if full:
            events.log("aviso", f"El pendrive {nombre} se llenó", "almacenamiento")
        else:
            events.log("error", f"Error escribiendo en el pendrive {nombre}: {exc}", "almacenamiento")
        with self._lock:
            if d:
                d["state"] = "lleno" if full else "error"
            self._choose()

    def eject(self, drive_id):
        """Marca un pendrive para quitar: deja de usarse y se desmonta."""
        d = self.drives.get(drive_id)
        if not d:
            return False
        self._blocked[drive_id] = ("expulsado", time.time())
        with self._lock:
            d["state"] = "expulsado"
            self._choose()
        return True

    def unmount(self, drive_id):
        d = self.drives.get(drive_id)
        if not d or not d.get("mount") or paths.SIM:
            return True
        os.sync()
        r = _run(["umount", d["mount"]], timeout=60)
        if r.returncode != 0:
            _run(["umount", "-l", d["mount"]])
        events.log("info", f"Pendrive {d['label'] or d['dev']} listo para quitar", "almacenamiento")
        return True

    def summary(self, bytes_per_day):
        with self._lock:
            drives = []
            free_total = 0
            for d in self.drives.values():
                usable = max(0, d["free"] - self.reserve) if d["state"] == "ok" else 0
                free_total += usable
                drives.append({
                    "id": d["id"], "nombre": d["label"] or Path(d["dev"]).name,
                    "fs": d["fstype"], "total_gb": round(d["total"] / 1e9, 1),
                    "libre_gb": round(d["free"] / 1e9, 1), "estado": d["state"],
                    "actual": d["id"] == self.current_id,
                })
        dias = free_total / bytes_per_day if bytes_per_day else 0
        return {"pendrives": drives, "libre_total_gb": round(free_total / 1e9, 1),
                "dias_restantes": round(dias, 1)}


def append_index(info, nombre):
    """Agrega el fragmento al índice CSV de esa carpeta del pendrive."""
    path = Path(info["archivo"]).parent.parent / "indice.csv"
    new = not path.exists()
    from datetime import datetime
    fmt = "%Y-%m-%d %H:%M:%S"
    line = ",".join([
        Path(info["archivo"]).parent.name + "/" + Path(info["archivo"]).name,
        datetime.fromtimestamp(info["inicio"]).strftime(fmt),
        datetime.fromtimestamp(info["fin"]).strftime(fmt),
        str(info["duracion_s"]), str(info["cuadros"]), str(info["fps_medio"]),
        str(round(info["bytes"] / 1e6, 1)), "si" if info["incompleto"] else "no",
    ])
    try:
        with open(path, "a") as f:
            if new:
                f.write("archivo,inicio,fin,duracion_s,cuadros,fps_medio,mb,incompleto\n")
            f.write(line + "\n")
    except OSError:
        pass
