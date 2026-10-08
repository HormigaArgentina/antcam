"""Marca en el video: fecha y hora (y opcionalmente lugar, especie, nota) abajo a la izquierda.

Se dibuja sobre el canal de luminancia (Y) de cada cuadro antes de codificarlo, así que queda
grabada en el video. El texto se dibuja con PIL una vez por segundo (cuando cambia la hora) y
en cada cuadro solo se copia ese pequeño recorte con numpy: no frena a la Pi.

Letras blancas con borde negro: se leen sobre tierra clara u oscura sin tapar con un recuadro.
"""

import threading
import time

import numpy as np

FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]
ALTO_RELATIVO = 0.032   # alto de la letra respecto del alto del cuadro ("pequeña")
LETRA_MIN = 11
BLANCO, NEGRO = 235, 16  # niveles de luminancia de video (rango limitado)
MAX_CAMPO = 40


def _font(px):
    from PIL import ImageFont
    for f in FONTS:
        try:
            return ImageFont.truetype(f, px), True
        except OSError:
            continue
    return ImageFont.load_default(), False


def texto_info(r):
    """Segunda línea: lugar · especie · nota (lo que esté completo)."""
    partes = [r.get(k, "").strip() for k in ("lugar", "especie", "nota")]
    return "  ·  ".join(p for p in partes if p)


class Rotulo:
    def __init__(self, cfg_rotulo=None):
        self._lock = threading.Lock()
        self._cfg = {}
        self._cache = {}       # (líneas, alto de letra) -> máscara
        self._fonts = {}
        self.configure(cfg_rotulo or {})

    # ---------------------------------------------------------------- configuración
    def configure(self, cfg_rotulo):
        with self._lock:
            self._cfg = dict(cfg_rotulo or {})
            self._cache.clear()

    @property
    def activo(self):
        c = self._cfg
        return bool(c.get("fecha_hora")) or bool(texto_info(c))

    def lineas(self, t=None):
        c = self._cfg
        out = []
        info = texto_info(c)
        if info:
            out.append(info)
        if c.get("fecha_hora"):
            out.append(time.strftime("%Y-%m-%d  %H:%M:%S", time.localtime(t or time.time())))
        return tuple(out)

    # ---------------------------------------------------------------- dibujo
    def _mascara(self, lineas, px):
        """Imagen 'L': 0 = transparente, valores altos = letra, intermedios = borde."""
        key = (lineas, px)
        m = self._cache.get(key)
        if m is not None:
            return m
        from PIL import Image, ImageDraw
        if px not in self._fonts:
            self._fonts[px] = _font(px)
        font, ttf = self._fonts[px]
        borde = max(1, px // 9) if ttf else 0
        gap = max(2, px // 5)
        sizes = []
        for ln in lineas:
            x0, y0, x1, y1 = font.getbbox(ln)
            sizes.append((x1, y1))
        alto_linea = max(px, max(h for _, h in sizes))
        W = max(w for w, _ in sizes) + 2 * borde + 2
        H = len(lineas) * alto_linea + (len(lineas) - 1) * gap + 2 * borde + 2
        img = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(img)
        y = borde
        for ln in lineas:
            if ttf:
                d.text((borde, y), ln, font=font, fill=255, stroke_width=borde, stroke_fill=60)
            else:  # fuente de emergencia sin borde: sombra corrida
                d.text((borde + 1, y + 1), ln, font=font, fill=60)
                d.text((borde, y), ln, font=font, fill=255)
            y += alto_linea + gap
        a = np.asarray(img)
        m = (a >= 150, (a > 0) & (a < 150))  # (letra, borde)
        if len(self._cache) > 8:
            self._cache.clear()
        self._cache[key] = m
        return m

    def aplicar(self, y, t=None):
        """Dibuja la marca sobre un plano de luminancia (alto, ancho), modificándolo en el lugar."""
        if not self.activo:
            return y
        h, w = y.shape[:2]
        if h < 40 or w < 80:
            return y
        with self._lock:
            lineas = self.lineas(t)
            if not lineas:
                return y
            px = max(LETRA_MIN, int(round(h * ALTO_RELATIVO)))
            letra, borde = self._mascara(lineas, px)
        mh, mw = letra.shape
        margen = max(4, px // 2)
        mh, mw = min(mh, h - margen), min(mw, w - margen)
        if mh <= 0 or mw <= 0:
            return y
        y0, x0 = h - margen - mh, margen
        reg = y[y0:y0 + mh, x0:x0 + mw]
        reg[borde[:mh, :mw]] = NEGRO
        reg[letra[:mh, :mw]] = BLANCO
        return y
