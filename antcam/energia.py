"""Registro de la alimentación (baja tensión).

La Raspberry Pi 3B+ no mide los volts de entrada: el firmware solo avisa si la tensión cae por
debajo de ~4.63 V. Acá se consulta ese aviso cada medio segundo y se arma:

- un historial de los últimos 30 min en tramos de 10 s (% del tiempo en baja tensión), que la
  página muestra como gráfico para poder regular la fuente en vivo;
- un registro CSV cada N minutos (energia.csv en la Pi y en el pendrive), para ver qué pasó
  durante días en el campo.

En placas que sí miden la entrada (Pi 5: `vcgencmd pmic_read_adc EXT5V_V`) se guardan también
los volts mínimos de cada tramo.
"""

import math
import re
import subprocess
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import paths

TRAMO_S = 10
VENTANA_S = 30 * 60
SYSFS = Path("/sys/devices/platform/soc/soc:firmware/get_throttled")
CABECERA = "inicio,fin,muestras,porcentaje_baja_tension,caidas,temperatura_max_c,volts_min\n"
MAX_BYTES = 2_000_000


def _hwmon_alarm():
    """Alarma de baja tensión del driver rpi_volt (si el kernel lo tiene)."""
    for d in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (d / "name").read_text().strip() == "rpi_volt" and (d / "in0_lcrit_alarm").exists():
                return d / "in0_lcrit_alarm"
        except OSError:
            continue
    return None


class Energia:
    def __init__(self, registro_min=5):
        self.registro_s = registro_min * 60
        self.tramos = deque(maxlen=VENTANA_S // TRAMO_S)  # [t0, muestras, bajas, volts_min]
        self.ahora = None
        self._prev = False
        self._ultimo_muestreo = 0
        self._ultimo_volts = 0
        self._volts = None
        self._fuente = None
        self._intervalo = 0.5
        self._periodo = self._nuevo_periodo(time.time())
        self._caidas_30 = deque()  # momentos en que empezó una baja de tensión
        if not paths.SIM:
            if SYSFS.exists():
                self._fuente = "sysfs"
            else:
                self._hwmon = _hwmon_alarm()
                if self._hwmon:
                    self._fuente = "hwmon"
                else:
                    self._fuente = "vcgencmd"
                    self._intervalo = 2.0  # cada consulta lanza un proceso: más espaciado
            self._mide_volts = self._leer_volts() is not None
        else:
            self._mide_volts = True

    # ---------------------------------------------------------------- lectura
    def _leer_baja(self):
        if paths.SIM:  # simulación: una caída de 15 s cada 2 minutos
            return (time.time() % 120) < 15
        try:
            if self._fuente == "sysfs":
                return bool(int(SYSFS.read_text().strip(), 16) & 0x1)
            if self._fuente == "hwmon":
                return self._hwmon.read_text().strip() == "1"
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True,
                                 timeout=5).stdout
            return bool(int(out.strip().split("=")[1], 16) & 0x1)
        except Exception:
            return None

    def _leer_volts(self):
        if paths.SIM:
            return round(5.05 + 0.08 * math.sin(time.time() / 40) - (0.5 if self._leer_baja() else 0), 2)
        try:
            out = subprocess.run(["vcgencmd", "pmic_read_adc", "EXT5V_V"], capture_output=True,
                                 text=True, timeout=5).stdout
            m = re.search(r"=\s*([\d.]+)V", out)
            return float(m.group(1)) if m else None
        except Exception:
            return None

    @staticmethod
    def _nuevo_periodo(t):
        return {"inicio": t, "muestras": 0, "bajas": 0, "caidas": 0, "temp": None, "volts": None}

    # ---------------------------------------------------------------- muestreo
    def muestrear(self, temp=None):
        """Llamar seguido (cada ~0.5 s). Devuelve el texto de un resumen si cerró un período."""
        now = time.time()
        if now - self._ultimo_muestreo < self._intervalo:
            return None
        self._ultimo_muestreo = now
        baja = self._leer_baja()
        if baja is None:
            return None
        self.ahora = baja
        if self._mide_volts and now - self._ultimo_volts >= 5:
            self._ultimo_volts = now
            self._volts = self._leer_volts()

        t0 = now - now % TRAMO_S
        if not self.tramos or self.tramos[-1][0] != t0:
            self.tramos.append([t0, 0, 0, None])
        tr = self.tramos[-1]
        tr[1] += 1
        tr[2] += int(baja)
        if self._volts is not None:
            tr[3] = self._volts if tr[3] is None else min(tr[3], self._volts)

        if baja and not self._prev:
            self._caidas_30.append(now)
        while self._caidas_30 and self._caidas_30[0] < now - VENTANA_S:
            self._caidas_30.popleft()

        p = self._periodo
        p["muestras"] += 1
        p["bajas"] += int(baja)
        p["caidas"] += int(baja and not self._prev)
        if temp is not None:
            p["temp"] = temp if p["temp"] is None else max(p["temp"], temp)
        if self._volts is not None:
            p["volts"] = self._volts if p["volts"] is None else min(p["volts"], self._volts)
        self._prev = baja

        if now - p["inicio"] >= self.registro_s:
            self._periodo = self._nuevo_periodo(now)
            return self._cerrar(p, now)
        return None

    def _cerrar(self, p, fin):
        fmt = "%Y-%m-%d %H:%M:%S"
        pct = 100 * p["bajas"] / p["muestras"] if p["muestras"] else 0
        self.ultimo_periodo = {
            "linea": ",".join([
                datetime.fromtimestamp(p["inicio"]).strftime(fmt), datetime.fromtimestamp(fin).strftime(fmt),
                str(p["muestras"]), f"{pct:.1f}", str(p["caidas"]),
                "" if p["temp"] is None else f"{p['temp']:.0f}",
                "" if p["volts"] is None else f"{p['volts']:.2f}",
            ]) + "\n",
            "pct": pct, "caidas": p["caidas"], "minutos": round((fin - p["inicio"]) / 60),
        }
        return self.ultimo_periodo

    # ---------------------------------------------------------------- archivo
    @staticmethod
    def escribir(path, linea):
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > MAX_BYTES:
                path.replace(path.with_suffix(".anterior.csv"))
            nuevo = not path.exists()
            with open(path, "a") as f:
                if nuevo:
                    f.write(CABECERA)
                f.write(linea)
        except OSError:
            pass

    # ---------------------------------------------------------------- estado para la página
    def resumen(self):
        now = time.time()
        tramos = [t for t in self.tramos if t[0] >= now - VENTANA_S]
        muestras = sum(t[1] for t in tramos)
        bajas = sum(t[2] for t in tramos)
        return {
            "ahora_baja": self.ahora,
            "mide_volts": self._mide_volts,
            "volts": self._volts,
            "tramo_s": TRAMO_S,
            "tramos": [[int(t[0]), round(t[2] / t[1], 3) if t[1] else 0, t[3]] for t in tramos],
            "pct_30min": round(100 * bajas / muestras, 1) if muestras else None,
            "caidas_30min": len(self._caidas_30),
            "registro_min": self.registro_s // 60,
        }
