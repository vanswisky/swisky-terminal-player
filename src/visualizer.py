"""
visualizer.py
=============
Realtime-feeling FFT spectrum analyzer.

Rather than tapping mpv's internal audio pipeline (which libmpv does
not expose easily for arbitrary read access), this module decodes the
track's raw PCM once up front with `soundfile` into a small rolling
buffer strategy: it keeps the full sample array in memory (mono-summed,
downsampled to a fixed analysis rate) and, every frame, windows out
the ~40ms slice corresponding to the *current mpv playback position*.
That slice is FFT'd, binned into log-spaced frequency bands, and
exponentially smoothed with peak-hold — giving bars that track what's
actually playing rather than a generic animation.

Decoding happens on a background thread so large FLACs don't block
track-start.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

import numpy as np

try:
    import soundfile as sf
    _HAS_SOUNDFILE = True
except ImportError:  # pragma: no cover
    _HAS_SOUNDFILE = False

from config import VisualizerConfig

logger = logging.getLogger(__name__)

ANALYSIS_SAMPLE_RATE = 22050  # downsample target — plenty for visual bands
WINDOW_SECONDS = 2048 / ANALYSIS_SAMPLE_RATE


@dataclass(slots=True)
class SpectrumFrame:
    bands: np.ndarray          # current smoothed magnitude per band, 0..1
    peaks: np.ndarray          # peak-hold markers per band, 0..1
    bands_right: np.ndarray | None = None  # stereo right channel (optional)


class SpectrumAnalyzer:
    """Decodes one track's PCM and serves FFT frames for arbitrary positions."""

    def __init__(self, config: VisualizerConfig) -> None:
        self.config = config
        self._mono: np.ndarray | None = None
        self._left: np.ndarray | None = None
        self._right: np.ndarray | None = None
        self._sample_rate = ANALYSIS_SAMPLE_RATE
        self._lock = threading.RLock()
        self._loading_path: str | None = None

        self._smoothed = np.zeros(config.band_count, dtype=np.float32)
        self._smoothed_right = np.zeros(config.band_count, dtype=np.float32)
        self._peaks = np.zeros(config.band_count, dtype=np.float32)
        self._peak_timestamps = np.zeros(config.band_count, dtype=np.float32)

        # Log-spaced band edges from ~40Hz to Nyquist give a visually
        # even spread (bass doesn't dominate half the display).
        self._band_edges = np.geomspace(40, ANALYSIS_SAMPLE_RATE / 2 - 1, config.band_count + 1)

    def load_async(self, path: str) -> None:
        if not _HAS_SOUNDFILE:
            logger.warning("soundfile not available; visualizer disabled")
            return
        self._loading_path = path
        thread = threading.Thread(target=self._decode, args=(path,), daemon=True)
        thread.start()

    def _decode(self, path: str) -> None:
        try:
            data, sr = sf.read(path, always_2d=True, dtype="float32")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Visualizer could not decode %s: %s", path, exc)
            with self._lock:
                self._mono = None
            return

        # Downsample (simple decimation is fine for visualization purposes).
        if sr > ANALYSIS_SAMPLE_RATE:
            factor = max(1, sr // ANALYSIS_SAMPLE_RATE)
            data = data[::factor]
            sr = sr // factor

        left = data[:, 0]
        right = data[:, 1] if data.shape[1] > 1 else data[:, 0]
        mono = data.mean(axis=1)

        with self._lock:
            if self._loading_path == path:
                self._mono = mono
                self._left = left
                self._right = right
                self._sample_rate = sr

    def analyze_at(self, position_seconds: float) -> SpectrumFrame:
        with self._lock:
            mono, left, right, sr = self._mono, self._left, self._right, self._sample_rate

        if mono is None:
            return SpectrumFrame(bands=self._smoothed.copy(), peaks=self._peaks.copy())

        window_len = 2048
        start = int(position_seconds * sr)
        end = start + window_len
        if start < 0 or start >= len(mono):
            target_bands = np.zeros(self.config.band_count, dtype=np.float32)
            target_right = np.zeros(self.config.band_count, dtype=np.float32)
        else:
            target_bands = self._fft_bands(mono[start:end], sr)
            target_right = (
                self._fft_bands(right[start:end], sr) if self.config.stereo else target_bands
            )

        alpha = 1.0 - self.config.smoothing
        self._smoothed = self._smoothed * self.config.smoothing + target_bands * alpha
        self._smoothed_right = (
            self._smoothed_right * self.config.smoothing + target_right * alpha
        )

        self._peaks = np.maximum(self._peaks * 0.94, self._smoothed)

        return SpectrumFrame(
            bands=self._smoothed.copy(),
            peaks=self._peaks.copy(),
            bands_right=self._smoothed_right.copy() if self.config.stereo else None,
        )

    def decay(self, falloff: float = 0.80) -> SpectrumFrame:
        """Call this instead of `analyze_at` while playback is paused or
        stopped. There's no new audio signal to analyze, so rather than
        re-reading the same frozen position (which just holds the bars
        static mid-air), every call multiplies the current bars toward
        zero — a cava-style gravity fall instead of a freeze-frame.
        """
        self._smoothed *= falloff
        self._smoothed_right *= falloff
        self._peaks *= falloff

        # Snap near-zero to exact zero so bars fully settle at the
        # baseline instead of asymptotically hovering just above it.
        floor = 0.01
        self._smoothed[self._smoothed < floor] = 0.0
        self._smoothed_right[self._smoothed_right < floor] = 0.0
        self._peaks[self._peaks < floor] = 0.0

        return SpectrumFrame(
            bands=self._smoothed.copy(),
            peaks=self._peaks.copy(),
            bands_right=self._smoothed_right.copy() if self.config.stereo else None,
        )

    def _fft_bands(self, chunk: np.ndarray, sr: int) -> np.ndarray:
        n = len(chunk)
        if n == 0:
            return np.zeros(self.config.band_count, dtype=np.float32)
        if n < 2048:
            chunk = np.pad(chunk, (0, 2048 - n))
        windowed = chunk * np.hanning(len(chunk))
        spectrum = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(len(windowed), d=1.0 / sr)

        bands = np.zeros(self.config.band_count, dtype=np.float32)
        for i in range(self.config.band_count):
            lo, hi = self._band_edges[i], self._band_edges[i + 1]
            mask = (freqs >= lo) & (freqs < hi)
            bands[i] = spectrum[mask].mean() if mask.any() else 0.0

        # Log-compress magnitude and normalize to 0..1 for display.
        bands = np.log1p(bands * 8.0)
        peak = bands.max()
        if peak > 1e-6:
            bands = bands / peak
        return np.clip(bands, 0.0, 1.0)

    def reset(self) -> None:
        self._smoothed[:] = 0
        self._smoothed_right[:] = 0
        self._peaks[:] = 0
