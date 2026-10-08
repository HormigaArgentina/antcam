"""Servicio de grabación (antcam-grabador).

Arranca solo al encender la Pi, graba en fragmentos, cambia de pendrive cuando uno se llena,
y vuelve a empezar solo si algo falla. El servicio web le manda órdenes con archivos en
/run/antcam/cmd y lee su estado en /run/antcam/estado.json.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from . import __version__, camera, config, events, paths
from .rotulo import Rotulo, texto_info
from .segment_output import SegmentingOutput
from .storage import Storage, append_index

SRC = "grabador"
FRAMING_TIMEOUT = 10 * 60        # el modo encuadre se cierra solo a los 10 min
STALL_S = 25                     # sin cuadros nuevos durante esto = cámara trabada
CAMERA_RETRY_S = 30
TEST_BITRATES = [2.0, 3.0, 5.0]
TEST_SECONDS = 60


# ------------------------------------------------------------------ utilidades del sistema
def sd_notify(msg):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError:
        pass


def cpu_temp():
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return None


def throttled():
    """Bits de la Pi: 0 baja tensión ahora, 16 hubo baja tensión, 2/18 limitación por calor."""
    for p in ("/sys/devices/platform/soc/soc:firmware/get_throttled",):
        try:
            return int(Path(p).read_text().strip(), 16)
        except (OSError, ValueError):
            pass
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True,
                             timeout=5).stdout
        return int(out.strip().split("=")[1], 16)
    except Exception:
        return None


def uptime_s():
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError):
        return 0.0


def write_json_atomic(path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


# ------------------------------------------------------------------ LEDs
class Leds:
    """LED verde de la placa: late = grabando; parpadeo rápido = problema; apagado = en pausa.

    LEDs externos (como en el equipo anterior): rojo fijo = encendido, verde parpadea = grabando,
    amarillo parpadea = problema.
    """

    def __init__(self, externos):
        self.board = None
        for name in ("ACT", "led0"):
            p = Path("/sys/class/leds") / name
            if p.exists():
                self.board = p
                break
        self.ext = None
        if externos and not paths.SIM:
            try:
                from gpiozero import LED
                self.ext = {"rojo": LED(14), "amarillo": LED(27), "verde": LED(22)}
                self.ext["rojo"].on()
            except Exception:
                self.ext = None
        self.mode = None

    def _board(self, trigger, on=None, off=None):
        if not self.board:
            return
        try:
            (self.board / "trigger").write_text(trigger)
            if trigger == "timer":
                (self.board / "delay_on").write_text(str(on))
                (self.board / "delay_off").write_text(str(off))
            elif trigger == "none":
                (self.board / "brightness").write_text("0")
        except OSError:
            pass

    def set(self, mode):
        if mode == self.mode:
            return
        self.mode = mode
        if mode == "grabando":
            self._board("heartbeat")
        elif mode == "problema":
            self._board("timer", 100, 100)
        else:
            self._board("none")
        if self.ext:
            v, a = self.ext["verde"], self.ext["amarillo"]
            v.off()
            a.off()
            if mode == "grabando":
                v.blink(0.2, 0.8)
            elif mode == "problema":
                a.blink(0.2, 0.2)


# ------------------------------------------------------------------ grabador
class Recorder:
    def __init__(self):
        paths.ensure_dirs()
        self.cfg = config.load()
        self.storage = Storage(self.cfg["nombre"], self.cfg["almacenamiento"]["reserva_gb"])
        self.leds = Leds(self.cfg["leds_externos"])
        self.rotulo = Rotulo(self.cfg["rotulo"])
        self.cam = None
        self.out = None
        self.mode = None                 # "grabando" | "encuadre" | "prueba" | None
        self.state = "iniciando"
        self.message = ""
        self.cam_error = None
        self.cam_retry_at = 0
        self.framing_until = 0
        self.test_queue = []
        self.test_current = None
        self.size = None
        self.rec_started = 0
        self.exposure_locked = False
        self.segments_total = 0
        self.segments_today = {}
        self.last_segment = None
        self.measured_bps = None
        self.stop_flag = threading.Event()
        self.lock = threading.RLock()
        self.alerts = {}
        self._last_storage = 0
        self._last_status = 0
        self._last_frames = 0

    # -------------------------------------------------------------- pipeline
    def _bytes_per_day(self):
        """Usa lo que realmente ocupan los fragmentos (la escena quieta comprime más);
        si todavía no hay ninguno, la tasa configurada."""
        if self.measured_bps:
            return self.measured_bps * 1.1 * 86400
        return self.cfg["grabacion"]["bitrate_mbps"] * 1e6 / 8 * 86400

    def _desired(self):
        if self.framing_until > time.time():
            return "encuadre"
        if self.test_queue or self.test_current:
            return "prueba"
        if self.cfg["grabacion"]["habilitada"]:
            return "grabando"
        return None

    def _open_camera(self):
        if self.cam is not None:
            return True
        if time.time() < self.cam_retry_at:
            return False
        try:
            self.cam = camera.open_camera()
            self.cam.rotulo = self.rotulo
            if self.cam_error:
                events.log("info", "La cámara volvió a responder", SRC)
            self.cam_error = None
            return True
        except Exception as e:
            msg = str(e)
            if msg != self.cam_error:
                events.log("error", f"Problema con la cámara: {msg}", SRC)
            self.cam_error = msg
            self.cam_retry_at = time.time() + CAMERA_RETRY_S
            self.cam = None
            return False

    def _stop_pipeline(self):
        with self.lock:
            if self.cam:
                try:
                    self.cam.stop()
                except Exception:
                    pass
            if self.out:
                try:
                    self.out.stop()  # cierra el fragmento abierto
                except Exception:
                    pass
            self.out = None
            self.mode = None

    def _make_output(self, segment_s, prefix, align, max_segments=None, subdir=None):
        def target():
            t = self.storage.target()
            if t and subdir:
                t = dict(t, dir=t["dir"] / subdir)
            return t

        return SegmentingOutput(
            size=(0, 0), fps=self.cfg["grabacion"]["fps"], segment_s=segment_s,
            get_target=target, prefix=prefix, align=align,
            on_closed=self._on_segment, on_error=self.storage.mark_error,
            max_segments=max_segments, get_metadata=self._metadata)

    def _metadata(self):
        """Datos que se guardan dentro de cada .mp4 (se ven en VLC → Información del códec)."""
        r = self.cfg["rotulo"]
        partes = [f"Equipo: {self.cfg['nombre']}"]
        for k, t in (("lugar", "Lugar"), ("especie", "Especie"), ("nota", "Nota")):
            if r.get(k):
                partes.append(f"{t}: {r[k]}")
        md = {"comment": "; ".join(partes), "encoder": f"AntCam {__version__}"}
        info = texto_info(r)
        if info:
            md["title"] = info
        return md

    def _start_pipeline(self, mode):
        if not self._open_camera():
            return
        g = self.cfg["grabacion"]
        nombre = config.slug(self.cfg["nombre"])
        try:
            with self.lock:
                if mode == "encuadre":
                    self.cam.start_framing(self.cfg)
                elif mode == "grabando":
                    self.out = self._make_output(g["segmento_min"] * 60, nombre, g["alinear_reloj"])
                    self.size = self.cam.start_recording(self.cfg, self.out)
                    events.log("info", f"Grabación iniciada: {self.size[0]}x{self.size[1]}, "
                                       f"{g['fps']} cuadros/s, {g['bitrate_mbps']} Mbps, "
                                       f"fragmentos de {g['segmento_min']} min", SRC)
                elif mode == "prueba":
                    b = self.test_queue.pop(0)
                    self.test_current = b
                    self.out = self._make_output(TEST_SECONDS, f"{nombre}_prueba_{b:g}Mbps", False,
                                                 max_segments=1, subdir="pruebas_calidad")
                    self.size = self.cam.start_recording(self.cfg, self.out, bitrate_mbps=b)
                    events.log("info", f"Prueba de calidad: grabando 1 minuto a {b:g} Mbps", SRC)
                self.mode = mode
                self.rec_started = time.time()
                self.exposure_locked = False
                self._last_frames = 0
        except Exception as e:
            events.log("error", f"No se pudo iniciar la cámara: {e}", SRC)
            self.cam_error = str(e)
            self._reset_camera()

    def _reset_camera(self):
        self._stop_pipeline()
        if self.cam:
            try:
                self.cam.close()
            except Exception:
                pass
        self.cam = None
        self.cam_retry_at = time.time() + CAMERA_RETRY_S

    def _on_segment(self, info):
        self.segments_total += 1
        day = time.strftime("%Y-%m-%d", time.localtime(info["inicio"]))
        self.segments_today = {day: self.segments_today.get(day, 0) + 1}
        self.last_segment = {k: info[k] for k in ("archivo", "duracion_s", "cuadros", "fps_medio")}
        if "pruebas_calidad" not in info["archivo"]:
            append_index(info, self.cfg["nombre"], self.cfg["rotulo"])
            if info["duracion_s"] > 30 and not info["incompleto"]:
                bps = info["bytes"] / info["duracion_s"]
                self.measured_bps = bps if not self.measured_bps else 0.7 * self.measured_bps + 0.3 * bps
        events.log("debug", f"Fragmento cerrado: {Path(info['archivo']).name} "
                            f"({info['duracion_s']:.0f} s, {info['fps_medio']} cuadros/s)", SRC,
                   tipo="fragmento", archivo=info["archivo"], incompleto=info["incompleto"])

    # -------------------------------------------------------------- órdenes del servicio web
    def _commands(self):
        try:
            files = sorted(paths.CMD_DIR.iterdir(), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for f in files:
            try:
                arg = f.read_text().strip()
                f.unlink()
            except OSError:
                continue
            name = f.name.split(".")[0]
            self._do(name, arg)

    def _do(self, name, arg):
        if name == "recargar":
            old = self.cfg
            self.cfg = config.load()
            self.storage.nombre = self.cfg["nombre"]
            self.storage.reserve = int(self.cfg["almacenamiento"]["reserva_gb"] * 1e9)
            if old["rotulo"] != self.cfg["rotulo"]:
                self.rotulo.configure(self.cfg["rotulo"])  # se aplica al instante, sin cortar
            if old["grabacion"] != self.cfg["grabacion"] or old["nombre"] != self.cfg["nombre"]:
                if self.mode in ("grabando", "prueba", "encuadre"):
                    self._stop_pipeline()  # se reinicia con la configuración nueva
                if old["grabacion"]["habilitada"] != self.cfg["grabacion"]["habilitada"]:
                    events.log("info", "Grabación activada" if self.cfg["grabacion"]["habilitada"]
                               else "Grabación detenida por el usuario", SRC)
        elif name == "encuadre_on":
            self.framing_until = time.time() + FRAMING_TIMEOUT
        elif name == "encuadre_off":
            self.framing_until = 0
        elif name == "prueba":
            self.test_queue = list(TEST_BITRATES)
            self.test_current = None
            if self.mode != "prueba":
                self._stop_pipeline()
        elif name == "expulsar":
            cur = self.storage.current_id
            if self.storage.eject(arg):
                if arg == cur and self.out:
                    self.out.rotate_now()
                    time.sleep(2.5)  # dejar que se cierre el fragmento en el próximo cuadro clave
                self.storage.unmount(arg)
        elif name == "salir":
            self.stop_flag.set()

    # -------------------------------------------------------------- salud
    def _alert_once(self, key, level, msg, every_s=3600):
        last = self.alerts.get(key, 0)
        if time.time() - last > every_s:
            self.alerts[key] = time.time()
            events.log(level, msg, SRC)

    def _health(self):
        t = cpu_temp()
        if t and t > 78:
            self._alert_once("temp", "aviso", f"Temperatura alta en la Pi: {t:.0f} °C. "
                                              "Revisar sombra y ventilación de la caja.")
        th = throttled()
        if th is not None and th & 0x1:
            self._alert_once("volt", "aviso", "Baja tensión de alimentación: revisar fuente o "
                                              "regulador (debe dar 5.1–5.2 V).", 6 * 3600)
        return t, th

    def _check_stall(self):
        if self.mode not in ("grabando", "prueba") or not self.out:
            return False
        if time.time() - self.rec_started < STALL_S:
            return False
        if self.out.waiting_storage:
            return False
        last = self.out.last_frame_mono
        return last and time.monotonic() - last > STALL_S

    # -------------------------------------------------------------- vista previa
    def _preview_loop(self):
        from PIL import Image
        while not self.stop_flag.is_set():
            try:
                fresh = time.time() - paths.PREVIEW_REQ.stat().st_mtime < 10
            except OSError:
                fresh = False
            if not fresh or not self.cam or not self.mode:
                time.sleep(0.5)
                continue
            try:
                with self.lock:
                    gray = self.cam.capture_preview() if self.cam else None
                if gray is not None:
                    if self.mode in ("grabando", "prueba"):  # que se vea igual que en el video
                        gray = np.array(gray)
                        self.rotulo.aplicar(gray)
                    tmp = paths.PREVIEW_FILE.with_suffix(".tmp")
                    Image.fromarray(gray).save(tmp, "JPEG", quality=75)
                    os.replace(tmp, paths.PREVIEW_FILE)
            except Exception:
                pass
            time.sleep(0.4)

    # -------------------------------------------------------------- estado
    def _status(self, temp, th):
        st = self.storage.summary(self._bytes_per_day())
        o = self.out
        g = self.cfg["grabacion"]
        cur = o.current if o else None
        fps_now = None
        if o:
            frames = o.frames_total
            dt = time.time() - self._last_status if self._last_status else 0
            if dt > 0 and self._last_frames:
                fps_now = round((frames - self._last_frames) / dt, 1)
            self._last_frames = frames
        data = {
            "version": __version__,
            "actualizado": time.time(),
            "estado": self.state,
            "mensaje": self.message,
            "modo": self.mode,
            "camara": {"modelo": getattr(self.cam, "model", None), "error": self.cam_error},
            "video": {
                "tamano": list(self.size) if self.size else None,
                "fps_config": g["fps"], "fps_actual": fps_now,
                "bitrate_mbps": g["bitrate_mbps"], "segmento_min": g["segmento_min"],
            },
            "fragmento_actual": {
                "archivo": str(cur["path"]), "inicio": cur["start_wall"],
                "mb": round(cur["bytes"] / 1e6, 1),
            } if cur else None,
            "ultimo_fragmento": self.last_segment,
            "fragmentos_total": self.segments_total,
            "fragmentos_hoy": self.segments_today.get(time.strftime("%Y-%m-%d"), 0),
            "cuadros_perdidos": o.frames_dropped if o else 0,
            "encuadre_hasta": self.framing_until if self.mode == "encuadre" else None,
            "prueba": {"actual": self.test_current, "pendientes": self.test_queue}
            if self.mode == "prueba" else None,
            "almacenamiento": st,
            "sistema": {
                "temperatura": temp, "uptime_s": uptime_s(),
                "baja_tension_ahora": bool(th & 0x1) if th is not None else None,
                "hubo_baja_tension": bool(th & 0x10000) if th is not None else None,
                "limitado_por_calor": bool(th & 0x4) if th is not None else None,
            },
        }
        write_json_atomic(paths.STATUS_FILE, data)

    def _update_state(self):
        if self.cam_error and not self.cam:
            self.state, self.message = "error", self.cam_error
        elif self.mode == "encuadre":
            self.state, self.message = "encuadre", "Modo encuadre: la grabación está en pausa"
        elif self.mode == "prueba":
            self.state, self.message = "prueba", f"Prueba de calidad a {self.test_current:g} Mbps"
        elif self.mode == "grabando":
            if self.out and self.out.waiting_storage or self.storage.current_id is None:
                self.state, self.message = "sin_espacio", "No hay pendrive con espacio libre"
            else:
                self.state, self.message = "grabando", ""
        elif not self.cfg["grabacion"]["habilitada"]:
            self.state, self.message = "pausado", "Grabación detenida"
        else:
            self.state, self.message = "iniciando", ""
        led = {"grabando": "grabando", "error": "problema", "sin_espacio": "problema",
               "prueba": "grabando"}.get(self.state, "pausa")
        self.leds.set(led)

    # -------------------------------------------------------------- bucle principal
    def run(self):
        events.rotate_if_big()
        up = uptime_s()
        if up < 300 and not paths.SIM:
            events.log("aviso", f"El equipo se encendió (o se reinició) hace {up:.0f} s", SRC)
        events.log("info", f"Servicio de grabación iniciado (versión {__version__})", SRC)
        signal.signal(signal.SIGTERM, lambda *a: self.stop_flag.set())
        threading.Thread(target=self._preview_loop, daemon=True, name="vista").start()
        sd_notify("READY=1")
        temp, th = None, None
        while not self.stop_flag.is_set():
            now = time.time()
            self._commands()

            if now - self._last_storage > 10:
                self._last_storage = now
                try:
                    self.storage.refresh()
                except Exception as e:
                    events.log("error", f"Error revisando pendrives: {e}", SRC)
                # si el pendrive elegido cambió (lleno, expulsado), cortar ya el fragmento
                cur = self.out.current if self.out else None
                if cur and cur["target_id"] != self.storage.current_id:
                    self.out.rotate_now()
                temp, th = self._health()

            # fin de una prueba de calidad
            if self.mode == "prueba" and self.out and self.out.finished.is_set():
                self._stop_pipeline()
                self.test_current = None
                if not self.test_queue:
                    events.log("info", "Prueba de calidad terminada: los videos están en la "
                                       "carpeta pruebas_calidad del pendrive", SRC)

            want = self._desired()
            if want != self.mode:
                self._stop_pipeline()
                if want:
                    self._start_pipeline(want)

            if (self.mode == "grabando" and self.cfg["grabacion"]["fijar_exposicion"]
                    and not self.exposure_locked and now - self.rec_started > 10):
                try:
                    exp, gain = self.cam.lock_exposure()
                    events.log("info", f"Exposición fijada ({exp} µs, ganancia {gain:.2f})", SRC)
                except Exception as e:
                    events.log("aviso", f"No se pudo fijar la exposición: {e}", SRC)
                self.exposure_locked = True

            if self._check_stall():
                events.log("error", "La cámara dejó de entregar imágenes: se reinicia el servicio", SRC)
                self._stop_pipeline()
                sys.exit(1)  # systemd vuelve a arrancar el servicio en 5 s

            self._update_state()
            if now - self._last_status >= 2:
                try:
                    self._status(temp, th)
                except Exception as e:
                    print("estado:", e, flush=True)
                self._last_status = now
            sd_notify("WATCHDOG=1")
            time.sleep(0.5)

        events.log("info", "Servicio de grabación detenido", SRC)
        self._stop_pipeline()
        if self.cam:
            self.cam.close()
        self.leds.set("pausa")


def main():
    Recorder().run()


if __name__ == "__main__":
    main()
