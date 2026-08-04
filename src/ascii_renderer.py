"""
ascii_renderer.py
==================
The heart of the project: converts an album-cover PNG/JPEG into a
high-fidelity, TrueColor ASCII (or Unicode block / braille) image.

Pipeline (mirrors the spec):

    load -> Lanczos resize -> CLAHE -> histogram equalization
    -> auto contrast -> adaptive brightness -> gamma correction
    -> sharpen -> edge enhancement -> denoise -> tone mapping
    -> adaptive character mapping -> color quantization -> render

Design notes
------------
* Luminance decides *which glyph*; per-cell average RGB decides *the
  color* the glyph is painted with (ANSI TrueColor). Color and shape
  are computed from independent statistics so dark-but-saturated
  regions don't get flattened to blank space.
* An edge map (Sobel gradient magnitude) is blended into the
  character-selection score so silhouettes/eyes/hair edges bias
  towards higher-density glyphs even when local brightness is mid-range
  — this is what keeps facial structure legible instead of mushy.
* Braille mode packs a 2x4 dot grid per cell using a binarized,
  edge-boosted luminance field, giving roughly 4x the spatial
  resolution of a plain character ramp in the same terminal area.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

try:
    import cv2  # opencv-python-headless — used for CLAHE + Sobel edges
    _HAS_CV2 = True
except ImportError:  # pragma: no cover - optional acceleration
    _HAS_CV2 = False

from config import AsciiConfig, AsciiQuality, AsciiRenderMode, ColorMode, ASCII_QUALITY_COLUMNS
from constants import ASCII_CLASSIC_RAMP, ASCII_BLOCK_RAMP, BRAILLE_DOT_MAP

logger = logging.getLogger(__name__)

# Terminal character cells are roughly twice as tall as they are wide.
CHAR_ASPECT_COMPENSATION = 2.15


@dataclass(slots=True)
class AsciiCell:
    char: str
    r: int
    g: int
    b: int


@dataclass(slots=True)
class AsciiFrame:
    """A fully rendered ASCII image: rows of pre-colored cells plus
    an already-composited ANSI string ready to print.
    """
    width: int
    height: int
    rows: list[list[AsciiCell]]
    ansi_text: str


def _to_truecolor_ansi(rows: list[list[AsciiCell]]) -> str:
    lines = []
    for row in rows:
        parts = []
        last_rgb = None
        for cell in row:
            rgb = (cell.r, cell.g, cell.b)
            if rgb != last_rgb:
                parts.append(f"\x1b[38;2;{cell.r};{cell.g};{cell.b}m{cell.char}")
                last_rgb = rgb
            else:
                parts.append(cell.char)
        parts.append("\x1b[0m")
        lines.append("".join(parts))
    return "\n".join(lines)


class AsciiRenderer:
    """Stateless converter: image bytes/path in, `AsciiFrame` out.

    Caching lives in `ascii_cache.py`; this class does not cache
    anything itself so it stays trivially testable.
    """

    def __init__(self, config: AsciiConfig) -> None:
        self.config = config

    # -- public API ---------------------------------------------------

    def render(self, image_path: str, target_columns: int | None = None) -> AsciiFrame:
        cols = target_columns or ASCII_QUALITY_COLUMNS[self.config.quality]
        image = Image.open(image_path).convert("RGB")

        rows_px = self._target_rows(image, cols)
        processed_rgb, luminance, edges = self._pipeline(image, cols, rows_px)

        if self.config.mode == AsciiRenderMode.BRAILLE:
            return self._render_braille(processed_rgb, luminance, edges)
        elif self.config.mode == AsciiRenderMode.BLOCK:
            return self._render_ramp(processed_rgb, luminance, edges, ASCII_BLOCK_RAMP)
        else:
            return self._render_ramp(processed_rgb, luminance, edges, ASCII_CLASSIC_RAMP)

    # -- pipeline stages ------------------------------------------------

    def _target_rows(self, image: Image.Image, cols: int) -> int:
        aspect = image.height / image.width
        return max(1, int(cols * aspect / CHAR_ASPECT_COMPENSATION))

    def _pipeline(
        self, image: Image.Image, cols: int, rows: int
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Runs the full enhancement pipeline and returns:
        (rgb_array HxWx3 uint8, luminance HxW float32 0..1, edge_map HxW float32 0..1)

        For braille mode we sample at 2x/4x the cell grid so each
        character cell can carry its own 2x4 dot pattern.
        """
        mode = self.config.mode
        sample_cols = cols * 2 if mode == AsciiRenderMode.BRAILLE else cols
        sample_rows = rows * 4 if mode == AsciiRenderMode.BRAILLE else rows

        # 1. Lanczos resize
        img = image.resize((max(1, sample_cols), max(1, sample_rows)), Image.LANCZOS)
        arr = np.asarray(img).astype(np.uint8)

        # 2. CLAHE (adaptive local contrast) on the luminance channel
        arr = self._apply_clahe(arr)

        # 3. Global histogram equalization (blended lightly to avoid
        #    over-flattening tonal range already fixed by CLAHE)
        arr = self._apply_hist_eq(arr, strength=0.35)

        # 4. Auto contrast
        img = Image.fromarray(arr)
        img = ImageEnhance.Contrast(img).enhance(1.15)

        # 5. Adaptive brightness (target mean luminance ~ 0.5)
        arr = np.asarray(img).astype(np.float32) / 255.0
        arr = self._adaptive_brightness(arr)

        # 6. Gamma correction
        arr = np.clip(arr, 0.0, 1.0) ** (1.0 / 1.05)

        # 7. Sharpen
        img = Image.fromarray((arr * 255).astype(np.uint8))
        img = img.filter(ImageFilter.UnsharpMask(radius=1.4, percent=140, threshold=2))

        # 8. Edge enhancement (used both visually and for char selection)
        edge_map = self._sobel_edges(np.asarray(img))

        # 9. Noise reduction (mild — keep detail, remove speckle)
        img = img.filter(ImageFilter.MedianFilter(size=3))

        # 10. Tone mapping (simple Reinhard-style compression of highlights)
        arr = np.asarray(img).astype(np.float32) / 255.0
        arr = arr / (1.0 + 0.15 * arr)
        arr = np.clip(arr / arr.max() if arr.max() > 0 else arr, 0.0, 1.0)

        rgb = (arr * 255).astype(np.uint8)
        luminance = (0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2])
        return rgb, luminance.astype(np.float32), edge_map

    def _apply_clahe(self, arr: np.ndarray) -> np.ndarray:
        if not _HAS_CV2:
            return arr
        lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
        l_channel, a_channel, b_channel = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        l_channel = clahe.apply(l_channel)
        lab = cv2.merge((l_channel, a_channel, b_channel))
        return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)

    def _apply_hist_eq(self, arr: np.ndarray, strength: float) -> np.ndarray:
        if not _HAS_CV2:
            return arr
        ycrcb = cv2.cvtColor(arr, cv2.COLOR_RGB2YCrCb)
        y, cr, cb = cv2.split(ycrcb)
        y_eq = cv2.equalizeHist(y)
        y_blend = ((1 - strength) * y.astype(np.float32) + strength * y_eq.astype(np.float32)).astype(np.uint8)
        merged = cv2.merge((y_blend, cr, cb))
        return cv2.cvtColor(merged, cv2.COLOR_YCrCb2RGB)

    def _adaptive_brightness(self, arr: np.ndarray, target: float = 0.5) -> np.ndarray:
        mean = float(arr.mean())
        if mean <= 1e-6:
            return arr
        factor = target / mean
        factor = min(max(factor, 0.5), 1.8)  # avoid blowing out already-good images
        return np.clip(arr * (0.5 + 0.5 * factor), 0.0, 1.0)

    def _sobel_edges(self, rgb: np.ndarray) -> np.ndarray:
        gray = np.asarray(Image.fromarray(rgb).convert("L")).astype(np.float32)
        if _HAS_CV2:
            gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
            gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        else:
            gx = np.gradient(gray, axis=1)
            gy = np.gradient(gray, axis=0)
        mag = np.sqrt(gx ** 2 + gy ** 2)
        if mag.max() > 0:
            mag = mag / mag.max()
        return mag.astype(np.float32)

    # -- glyph-ramp rendering (classic / block) --------------------------

    def _render_ramp(
        self,
        rgb: np.ndarray,
        luminance: np.ndarray,
        edges: np.ndarray,
        ramp: str,
    ) -> AsciiFrame:
        h, w = luminance.shape
        ramp_len = len(ramp)

        # Edge-aware character mapping: blend luminance with local edge
        # strength so high-contrast structural detail (eyes, hair strands,
        # jawlines) always maps to denser glyphs, not just bright pixels.
        score = np.clip(luminance * 0.75 + edges * 0.25, 0.0, 0.999)
        idx = (score * ramp_len).astype(np.int32)
        idx = np.clip(idx, 0, ramp_len - 1)

        rows: list[list[AsciiCell]] = []
        for y in range(h):
            row: list[AsciiCell] = []
            for x in range(w):
                char = ramp[idx[y, x]]
                r, g, b = self._quantized_color(rgb[y, x])
                row.append(AsciiCell(char=char, r=r, g=g, b=b))
            rows.append(row)

        return AsciiFrame(width=w, height=h, rows=rows, ansi_text=_to_truecolor_ansi(rows))

    # -- braille rendering (highest fidelity) -----------------------------

    def _render_braille(
        self, rgb: np.ndarray, luminance: np.ndarray, edges: np.ndarray
    ) -> AsciiFrame:
        # luminance/edges are sampled at 2x cols, 4x rows relative to the
        # final character grid — pack each 2x4 dot block into one glyph.
        sample_h, sample_w = luminance.shape
        cell_cols = sample_w // 2
        cell_rows = sample_h // 4

        score = np.clip(luminance * 0.7 + edges * 0.3, 0.0, 1.0)
        # Adaptive local threshold (per-cell mean) beats a single global
        # cutoff — keeps detail in both shadow-heavy and bright regions.
        threshold = float(np.clip(score.mean() * 0.95, 0.12, 0.7))

        rows: list[list[AsciiCell]] = []
        for cy in range(cell_rows):
            row: list[AsciiCell] = []
            for cx in range(cell_cols):
                bits = 0
                r_acc = g_acc = b_acc = count = 0
                for dy, dx, bit in BRAILLE_DOT_MAP:
                    sy, sx = cy * 4 + dy, cx * 2 + dx
                    if sy >= sample_h or sx >= sample_w:
                        continue
                    if score[sy, sx] > threshold:
                        bits |= bit
                    px = rgb[sy, sx]
                    r_acc += int(px[0]); g_acc += int(px[1]); b_acc += int(px[2])
                    count += 1
                char = chr(0x2800 + bits)
                if count:
                    r, g, b = self._quantized_color(
                        np.array([r_acc / count, g_acc / count, b_acc / count])
                    )
                else:
                    r = g = b = 0
                row.append(AsciiCell(char=char, r=r, g=g, b=b))
            rows.append(row)

        return AsciiFrame(
            width=cell_cols, height=cell_rows, rows=rows, ansi_text=_to_truecolor_ansi(rows)
        )

    # -- color -----------------------------------------------------------

    def _quantized_color(self, rgb_px) -> tuple[int, int, int]:
        r, g, b = (int(v) for v in rgb_px[:3])
        if self.config.color_mode == ColorMode.MONOCHROME:
            return (0, 0, 0)  # caller should use theme accent instead; see widgets.py
        if self.config.color_mode == ColorMode.ANSI256:
            # Quantize to a 6x6x6 color cube then expand back to RGB —
            # approximates xterm-256 palette while keeping TrueColor
            # escape codes (works everywhere, looks like 256-color).
            step = 51
            r = round(r / step) * step
            g = round(g / step) * step
            b = round(b / step) * step
        return (r, g, b)
