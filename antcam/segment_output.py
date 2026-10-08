"""Salida de video en fragmentos MP4.

- Recibe los cuadros H.264 del codificador de la Pi con su marca de tiempo real.
- Corta un fragmento nuevo en cada cuadro clave (1 por segundo) cuando llega la hora, así
  no se pierde ningún cuadro entre fragmentos.
- Escribe MP4 *fragmentado*: si se corta la luz, el archivo queda reproducible hasta el
  último segundo grabado (el MP4 común queda inservible).
- Las marcas de tiempo son las reales del sensor: si de noche la cámara baja los cuadros por
  segundo, el video sigue durando lo que realmente duró.
- Escribe en un hilo aparte con una cola, para que un pendrive lento no frene a la cámara.
"""

import os
import queue
import threading
import time
from datetime import datetime, timedelta
from fractions import Fraction

import av

try:  # en la Pi heredamos de la clase de picamera2; en simulación no hace falta
    from picamera2.outputs import Output as _Base
except Exception:  # pragma: no cover
    class _Base:
        def __init__(self, *a, **k):
            self.recording = False
            self.needs_pacing = False
            self.needs_add_stream = False

        def _add_stream(self, *a, **k):
            pass

FAT_LIMIT = 3_900_000_000  # FAT32 no admite archivos de 4 GB


def split_nals(data):
    """Separa un bloque H.264 Annex-B en unidades NAL (sin códigos de inicio)."""
    out = []
    i, n = 0, len(data)
    starts = []
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0:
            break
        starts.append(j + 3)
        i = j + 3
    for k, s in enumerate(starts):
        e = starts[k + 1] - 3 if k + 1 < len(starts) else n
        nal = data[s:e]
        if k + 1 < len(starts) and nal.endswith(b"\x00"):
            nal = nal[:-1]  # cero del código de inicio de 4 bytes siguiente
        if nal:
            out.append(nal)
    return out


def headers_from_keyframe(data):
    sps = pps = None
    for nal in split_nals(data):
        t = nal[0] & 0x1F
        if t == 7 and sps is None:
            sps = nal
        elif t == 8 and pps is None:
            pps = nal
    if sps and pps:
        return b"\x00\x00\x00\x01" + sps + b"\x00\x00\x00\x01" + pps
    return None


def next_boundary(now, seg_s, align):
    if not align:
        return now + seg_s
    dt = datetime.fromtimestamp(now)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    since = (dt - midnight).total_seconds()
    k = int(since // seg_s) + 1
    b = (midnight + timedelta(seconds=k * seg_s)).timestamp()
    if b - now < 0.25 * seg_s and seg_s >= 300:
        # fragmento inicial demasiado corto: lo unimos al siguiente
        b += seg_s
    return b


class SegmentingOutput(_Base):
    def __init__(self, size, fps, segment_s, get_target, prefix,
                 align=True, on_closed=None, on_error=None, max_segments=None, get_metadata=None):
        """
        get_target() -> dict(dir=Path, id=str, fstype=str) o None si no hay dónde grabar.
        on_closed(info) se llama al cerrar cada fragmento.
        on_error(target_id, exc) se llama si falla la escritura (pendrive lleno/quitado).
        max_segments: si se indica, deja de grabar después de esa cantidad (pruebas).
        """
        super().__init__()
        self.size = size
        self.fps = fps
        self.segment_s = segment_s
        self.get_target = get_target
        self.prefix = prefix
        self.align = align
        self.on_closed = on_closed
        self.on_error = on_error
        self.max_segments = max_segments
        self.get_metadata = get_metadata

        self._q = queue.Queue(maxsize=max(60, int(fps * 20)))
        self._thread = None
        self._drop_until_key = False
        self._rotate = threading.Event()
        self._lock = threading.Lock()

        # estado público (lo lee el grabador)
        self.frames_total = 0
        self.frames_dropped = 0
        self.last_frame_mono = 0.0
        self.current = None  # dict con datos del fragmento abierto
        self.segments_done = 0
        self.finished = threading.Event()
        self.waiting_storage = False

        self._c = None  # contenedor PyAV abierto

    # --- interfaz de picamera2 -------------------------------------------------
    def start(self):
        self.recording = True
        self._thread = threading.Thread(target=self._writer, name="escritor", daemon=True)
        self._thread.start()

    def stop(self):
        self.recording = False
        if self._thread:
            self._q.put(None)
            self._thread.join(timeout=30)
            self._thread = None

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
        if audio or not self.recording:
            return
        if timestamp is None:
            timestamp = int(time.monotonic() * 1_000_000)
        self.last_frame_mono = time.monotonic()
        if self._drop_until_key and not keyframe:
            self.frames_dropped += 1
            return
        try:
            self._q.put_nowait((bytes(frame), bool(keyframe), int(timestamp), time.time()))
            self._drop_until_key = False
        except queue.Full:
            self.frames_dropped += 1
            self._drop_until_key = True

    # --- control -----------------------------------------------------------------
    def rotate_now(self):
        """Pide cerrar el fragmento actual en el próximo cuadro clave."""
        self._rotate.set()

    # --- hilo escritor -----------------------------------------------------------
    def _writer(self):
        need_key = True
        while True:
            item = self._q.get()
            if item is None:
                break
            data, key, ts, wall = item
            if self.finished.is_set():
                continue
            if key:
                if self._c is None or self._should_rotate(wall):
                    self._close()
                    if self.max_segments and self.segments_done >= self.max_segments:
                        self.finished.set()
                        continue
                    self._open(data, ts, wall)
                need_key = self._c is None
            if need_key or self._c is None:
                continue
            try:
                self._mux(data, key, ts)
            except Exception as e:  # pendrive lleno, quitado, error de E/S
                tid = self.current["target_id"] if self.current else None
                self._close(failed=True)
                need_key = True
                if self.on_error:
                    self.on_error(tid, e)
        self._close()

    def _should_rotate(self, wall):
        cur = self.current
        if self._rotate.is_set():
            return True
        if wall >= cur["deadline"] or wall < cur["start_wall"] - 5:  # hora corrida hacia atrás
            return True
        if cur["fstype"] == "vfat" and cur["bytes"] > FAT_LIMIT:
            return True
        return False

    def _open(self, keydata, ts, wall):
        self._rotate.clear()
        target = self.get_target()
        if not target:
            self.waiting_storage = True
            return
        self.waiting_storage = False
        start = datetime.fromtimestamp(wall)
        day_dir = target["dir"] / start.strftime("%Y-%m-%d")
        name = f"{self.prefix}_{start.strftime('%Y%m%d_%H%M%S')}.mp4"
        path = day_dir / name
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
            c = av.open(str(path), "w", format="mp4",
                        options={"movflags": "frag_keyframe+empty_moov+default_base_moof"})
            if self.get_metadata:
                try:
                    c.metadata.update(self.get_metadata())
                except Exception:
                    pass
            st = c.add_stream("h264", rate=self.fps)
            st.codec_context.width, st.codec_context.height = self.size
            st.time_base = Fraction(1, 90000)
            extradata = headers_from_keyframe(keydata)
            if extradata:
                try:
                    st.codec_context.extradata = extradata
                except Exception:
                    pass
        except Exception as e:
            if self.on_error:
                self.on_error(target["id"], e)
            return
        self._c = (c, st)
        self.current = {
            "path": path, "target_id": target["id"], "fstype": target.get("fstype", ""),
            "start_wall": wall, "first_ts": ts, "last_ts": ts, "frames": 0, "bytes": 0,
            "deadline": next_boundary(wall, self.segment_s, self.align),
        }

    def _mux(self, data, key, ts):
        c, st = self._c
        cur = self.current
        pts = ts - cur["first_ts"]
        if cur["frames"] and pts <= cur["last_pts"]:
            pts = cur["last_pts"] + 1  # nunca repetir marcas de tiempo
        pkt = av.Packet(data)
        pkt.pts = pkt.dts = pts
        pkt.time_base = Fraction(1, 1_000_000)
        pkt.stream = st
        try:
            pkt.is_keyframe = key
        except Exception:
            pass
        c.mux(pkt)
        cur["last_pts"] = pts
        cur["last_ts"] = ts
        cur["frames"] += 1
        cur["bytes"] += len(data)
        self.frames_total += 1

    def _close(self, failed=False):
        if self._c is None:
            return
        c, _ = self._c
        self._c = None
        cur, self.current = self.current, None
        try:
            c.close()
        except Exception:
            failed = True
        if cur["frames"] == 0:
            try:
                os.remove(cur["path"])
            except OSError:
                pass
            return
        self.segments_done += 1
        dur = (cur["last_ts"] - cur["first_ts"]) / 1e6
        if cur["frames"] > 1:
            dur += dur / (cur["frames"] - 1)  # sumar la duración del último cuadro
        info = {
            "archivo": str(cur["path"]), "destino": cur["target_id"],
            "inicio": cur["start_wall"], "fin": cur["start_wall"] + dur,
            "duracion_s": round(dur, 2), "cuadros": cur["frames"],
            "fps_medio": round(cur["frames"] / dur, 2) if dur > 0 else 0,
            "bytes": cur["bytes"], "incompleto": failed,
        }
        if self.on_closed:
            try:
                self.on_closed(info)
            except Exception:
                pass
