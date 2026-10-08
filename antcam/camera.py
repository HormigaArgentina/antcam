"""Cámara: versión real (picamera2, Raspberry Pi) y versión simulada para probar en la PC."""

import threading
import time
from fractions import Fraction

import numpy as np

from . import config, paths

PREVIEW_W = 640


def _preview_size(size):
    w, h = size
    pw = min(PREVIEW_W, w)
    ph = int(round(pw * h / w / 2)) * 2
    return pw, ph


def _crop_rect(zona, rotar):
    """La zona se elige sobre la imagen ya rotada; el sensor trabaja sin rotar."""
    x, y, w, h = zona
    if rotar:
        x, y = 1 - x - w, 1 - y - h
    return x, y, w, h


class PiCamera:
    """Cámara real con picamera2 + codificador H.264 por hardware."""

    def __init__(self):
        from picamera2 import Picamera2
        if not Picamera2.global_camera_info():
            raise RuntimeError("No se detecta ninguna cámara. Revisar el cable plano y que esté bien insertado.")
        self.Picamera2 = Picamera2
        self.cam = Picamera2()
        props = self.cam.camera_properties
        self.model = props.get("Model", "?")
        self.sensor_size = tuple(props.get("PixelArraySize", (3280, 2464)))
        self.mode_size = self._pick_mode()
        self.encoder = None
        self.preview_size = None
        self.running = None  # "grabando" | "encuadre" | None
        self.rotulo = None   # marca de fecha/hora (rotulo.Rotulo), la pone el grabador
        self._main = None    # tamaño y paso de línea del flujo principal

    def _pick_mode(self):
        """Modo del sensor con campo de visión completo y al menos 30 cuadros/s (en la v2: 1640x1232)."""
        sw = self.sensor_size[0]
        best = None
        for m in self.cam.sensor_modes:
            crop = m.get("crop_limits", (0, 0, 0, 0))
            if crop[2] < sw * 0.95 or m.get("fps", 0) < 29:
                continue
            if best is None or m["size"][0] > best["size"][0]:
                best = m
        if best is None:
            best = max(self.cam.sensor_modes, key=lambda m: m.get("crop_limits", (0, 0, 0, 0))[2])
        return tuple(best["size"])

    def _configure(self, size, fps, rotar):
        from libcamera import Transform
        self.preview_size = _preview_size(size)
        fd = int(1_000_000 / fps)
        kwargs = dict(
            main={"size": size, "format": "YUV420"},
            lores={"size": self.preview_size, "format": "YUV420"},
            controls={"FrameDurationLimits": (fd, fd)},
            transform=Transform(hflip=int(rotar), vflip=int(rotar)),
            buffer_count=6,
        )
        try:
            cfg = self.cam.create_video_configuration(sensor={"output_size": self.mode_size}, **kwargs)
        except TypeError:  # picamera2 antiguo
            cfg = self.cam.create_video_configuration(raw={"size": self.mode_size}, **kwargs)
        self.cam.configure(cfg)
        self.preview_size = tuple(cfg["lores"]["size"])
        m = self.cam.camera_configuration()["main"]
        self._main = (tuple(m["size"]), m.get("stride") or m["size"][0])
        return tuple(cfg["main"]["size"])

    def _set_crop(self, zona, rotar):
        _, maxr, _ = self.cam.camera_controls["ScalerCrop"]
        x0, y0, W, H = maxr
        fx, fy, fw, fh = _crop_rect(zona, rotar)
        rect = (int(x0 + fx * W), int(y0 + fy * H), int(fw * W), int(fh * H))
        self.cam.set_controls({"ScalerCrop": rect})

    def start_recording(self, cfg, output, bitrate_mbps=None):
        from picamera2.encoders import H264Encoder
        g = cfg["grabacion"]
        size = config.output_size(cfg, *self.sensor_size)
        size = self._configure(size, g["fps"], g["rotar_180"])
        output.size = size
        self._set_crop(g["zona"], g["rotar_180"])
        mbps = bitrate_mbps or g["bitrate_mbps"]
        self.encoder = H264Encoder(bitrate=int(mbps * 1_000_000), repeat=True, iperiod=g["fps"])
        self.cam.pre_callback = self._stamp
        self.cam.start()
        self.cam.start_encoder(self.encoder, output)
        self.running = "grabando"
        return size

    def start_framing(self, cfg):
        g = cfg["grabacion"]
        self._configure((1280, 960), 15, g["rotar_180"])
        self._set_crop([0, 0, 1, 1], False)
        self.cam.pre_callback = None
        self.cam.start()
        self.running = "encuadre"

    def _stamp(self, request):
        """Antes de codificar cada cuadro: dibuja la fecha/hora en el plano Y (luminancia)."""
        r = self.rotulo
        if r is None or not r.activo or not self._main:
            return
        try:
            from picamera2 import MappedArray
            (w, h), stride = self._main
            with MappedArray(request, "main") as m:
                y = m.array.reshape(-1)[:stride * h].reshape(h, stride)[:, :w]
                r.aplicar(y)
        except Exception as e:
            if not getattr(self, "_stamp_err", None):
                self._stamp_err = str(e)
                print("marca de fecha:", e, flush=True)

    def stop(self):
        try:
            if self.encoder is not None:
                self.cam.stop_encoder()
        except Exception:
            pass
        self.encoder = None
        try:
            self.cam.stop()
        except Exception:
            pass
        self.cam.pre_callback = None
        self.running = None

    def close(self):
        self.stop()
        try:
            self.cam.close()
        except Exception:
            pass

    def capture_preview(self):
        if not self.running:
            return None
        arr = self.cam.capture_array("lores", wait=3)
        w, h = self.preview_size
        return np.ascontiguousarray(arr[:h, :w])  # canal de luminancia (gris)

    def lock_exposure(self):
        md = self.cam.capture_metadata(wait=3)
        ctrl = {"AeEnable": False, "AwbEnable": False}
        for k in ("ExposureTime", "AnalogueGain", "ColourGains"):
            if k in md:
                ctrl[k] = md[k]
        self.cam.set_controls(ctrl)
        return md.get("ExposureTime"), md.get("AnalogueGain")


class FakeCamera:
    """Cámara simulada: dibuja un camino con 'hormigas' y codifica H.264 con libx264."""

    model = "simulada"
    sensor_size = (3280, 2464)

    def __init__(self):
        self.running = None
        self.rotulo = None
        self._thread = None
        self._stop = threading.Event()
        self._cfg = None
        self._zona = [0, 0, 1, 1]
        self.preview_size = (640, 480)
        rng = np.random.default_rng(1)
        self._ants = rng.random((40, 2))  # posición en el camino, carril
        self._dirs = np.where(rng.random(40) > 0.5, 1, -1)
        self._t0 = time.time()

    def _scene(self, size, zona):
        W, H = 820, 616
        t = time.time() - self._t0
        img = np.full((H, W), 110, np.uint8)
        yy = np.arange(H)[:, None]
        xx = np.arange(W)[None, :]
        trail = np.abs((yy - H * 0.2) - (xx * 0.6)) < 40
        img[trail] = 150
        for (p, lane), d in zip(self._ants, self._dirs):
            s = (p + d * t * 0.02) % 1.0
            x = int(s * W)
            y = int(H * 0.2 + x * 0.6 + (lane - 0.5) * 50)
            if 0 <= y < H:
                img[max(0, y - 3):y + 3, max(0, x - 4):x + 4] = 20
        x, y, w, h = zona
        crop = img[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)]
        from PIL import Image
        return np.asarray(Image.fromarray(crop).resize(size))

    def start_recording(self, cfg, output, bitrate_mbps=None):
        g = cfg["grabacion"]
        size = config.output_size(cfg, *self.sensor_size)
        output.size = size
        self._zona = g["zona"]
        self.preview_size = _preview_size(size)
        self._stop.clear()
        mbps = bitrate_mbps or g["bitrate_mbps"]
        self._thread = threading.Thread(target=self._loop, args=(size, g["fps"], mbps, output),
                                        daemon=True)
        output.start()
        self._thread.start()
        self.running = "grabando"
        return size

    def _loop(self, size, fps, mbps, output):
        import av
        enc = av.CodecContext.create("libx264", "w")
        enc.width, enc.height = size
        enc.pix_fmt = "yuv420p"
        enc.time_base = Fraction(1, fps)
        enc.bit_rate = int(mbps * 1e6)
        enc.options = {"preset": "ultrafast", "tune": "zerolatency", "g": str(fps), "bf": "0",
                       "x264-params": "repeat-headers=1"}
        n = 0
        nxt = time.monotonic()
        while not self._stop.is_set():
            gray = np.array(self._scene(size, self._zona))
            if self.rotulo is not None:
                self.rotulo.aplicar(gray)
            frame = av.VideoFrame.from_ndarray(np.stack([gray] * 3, -1), format="rgb24").reformat(
                format="yuv420p")
            frame.pts = n
            ts = int(time.monotonic() * 1e6)
            for p in enc.encode(frame):
                output.outputframe(bytes(p), p.is_keyframe, ts)
            n += 1
            nxt += 1 / fps
            time.sleep(max(0, nxt - time.monotonic()))
        output.stop()

    def start_framing(self, cfg):
        self._zona = [0, 0, 1, 1]
        self.preview_size = (640, 480)
        self.running = "encuadre"

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        self.running = None

    def close(self):
        self.stop()

    def capture_preview(self):
        if not self.running:
            return None
        time.sleep(0.1)
        return self._scene(self.preview_size, self._zona)

    def lock_exposure(self):
        return 10000, 1.0


def open_camera():
    return FakeCamera() if paths.SIM else PiCamera()
