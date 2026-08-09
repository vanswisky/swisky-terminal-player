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

Online tracks (`track.path` is an http(s) stream URL, not a local
file) can't be handed to `soundfile` directly — libsndfile has no
HTTP support, and YouTube's CDN 403s without the same User-Agent/
Referer headers mpv itself needs (see `audio_engine.py`'s `load()`
docstring). `load_async` downloads such a URL to a small scratch temp
file first (same headers as playback), decodes that, then deletes it
— a few seconds' extra wait the first time a track starts, same as
mpv's own initial buffering, not a per-frame cost.

`soundfile` (libsndfile) also can't decode the *codec* almost every
online track actually downloads as: `online_source.resolve()` asks
yt-dlp for `bestaudio[ext=m4a]/bestaudio/best`, which resolves to an
AAC/M4A stream (or Opus/WebM when m4a isn't offered) essentially every
time — libsndfile has no AAC/Opus decoder, so `sf.read()` raises for
every single online or imported-playlist track, `_mono` is left
`None`, and the spectrum silently sits flat/decaying forever. This is
invisible in day-to-day local-library use because those files are
overwhelmingly MP3/FLAC, which libsndfile does read — the gap only
shows up once something is actually streamed. `_decode` below falls
back to `audioread` (already a declared dependency in
requirements.txt — decodes via whatever the system already has,
ffmpeg/gstreamer/etc., i.e. the same codecs mpv itself can play) for
exactly this case, instead of giving up the moment `soundfile` fails.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

try:
    import soundfile as sf
    _HAS_SOUNDFILE = True
except ImportError:  # pragma: no cover
    _HAS_SOUNDFILE = False

try:
    import audioread
    _HAS_AUDIOREAD = True
except ImportError:  # pragma: no cover
    _HAS_AUDIOREAD = False

from config import VisualizerConfig

logger = logging.getLogger(__name__)

ANALYSIS_SAMPLE_RATE = 22050  # downsample target — plenty for visual bands
WINDOW_SECONDS = 2048 / ANALYSIS_SAMPLE_RATE

# Safety cap on how much of an online stream gets downloaded for
# analysis — comfortably more than any normal song at any bitrate, but
# stops a misidentified livestream (or a stream that never ends) from
# reading forever on a background thread.
_MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024


def _is_url(path: str) -> bool:
    return path.startswith("http://") or path.startswith("https://")


def _decode_with_audioread(path: str) -> tuple[np.ndarray, int]:
    """Fallback PCM decode for anything `soundfile` can't open — in
    practice this is the codec path for every online/playlist track
    (see module docstring). `audioread` hands back fixed-size chunks
    of interleaved signed 16-bit PCM bytes; this concatenates them and
    reshapes into the same `(frames, channels) float32 in [-1, 1]`
    shape `soundfile.read(..., always_2d=True)` produces, so nothing
    downstream of this needs to know which decoder actually ran.
    """
    with audioread.audio_open(path) as f:
        sr = f.samplerate
        channels = max(1, f.channels)
        raw = bytearray()
        for block in f:
            raw.extend(block)

    if not raw:
        raise RuntimeError("audioread decoded zero samples")

    samples = np.frombuffer(bytes(raw), dtype="<i2").astype(np.float32) / 32768.0
    # audioread's last block can be short of a full frame across all
    # channels — trim to a whole number of frames before reshaping.
    frame_count = len(samples) // channels
    samples = samples[: frame_count * channels].reshape(frame_count, channels)
    return samples, sr


def _headers_to_dict(header_list: list[str] | None) -> dict[str, str]:
    """`track.stream_headers` is a list of `"Key: Value"` strings (see
    `online_source.resolve` / `audio_engine.py`'s `load()` docstring
    for why it's that shape rather than a dict already) — this just
    reshapes it for `urllib.request.Request`.
    """
    headers: dict[str, str] = {}
    for entry in header_list or []:
        if ":" in entry:
            key, _, value = entry.partition(":")
            headers[key.strip()] = value.strip()
    return headers


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

    def load_async(self, path: str, http_headers: list[str] | None = None) -> None:
        if not _HAS_SOUNDFILE and not _HAS_AUDIOREAD:
            logger.warning("Neither soundfile nor audioread available; visualizer disabled")
            return
        self._loading_path = path
        thread = threading.Thread(target=self._decode, args=(path, http_headers), daemon=True)
        thread.start()

    def _download_to_temp(self, url: str, http_headers: list[str] | None) -> str | None:
        """Streams an online track's audio to a scratch temp file (see
        module docstring for why). Deleted again by `_decode` right
        after decoding — this is scratch space for one FFT pass, not a
        cache, so it never touches `session_cleanup.py`.
        """
        headers = _headers_to_dict(http_headers) or {"User-Agent": "Mozilla/5.0"}
        req = urllib.request.Request(url, headers=headers)
        tmp_path: str | None = None
        try:
            suffix = Path(urlparse(url).path).suffix or ".audio"
            fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="swisky-vis-")
            with urllib.request.urlopen(req, timeout=15) as resp, open(fd, "wb") as f:
                written = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    remaining = _MAX_DOWNLOAD_BYTES - written
                    if remaining <= 0:
                        logger.debug("Visualizer download for %s hit the size cap; truncating", url)
                        break
                    if len(chunk) > remaining:
                        chunk = chunk[:remaining]
                    f.write(chunk)
                    written += len(chunk)
            return tmp_path
        except Exception as exc:  # noqa: BLE001 — no spectrum beats a crashed background thread
            logger.debug("Visualizer could not fetch %s for analysis: %s", url, exc)
            if tmp_path is not None:
                Path(tmp_path).unlink(missing_ok=True)
            return None

    def _decode(self, path: str, http_headers: list[str] | None = None) -> None:
        local_path = path
        temp_path: str | None = None
        try:
            if _is_url(path):
                temp_path = self._download_to_temp(path, http_headers)
                if temp_path is None:
                    with self._lock:
                        if self._loading_path == path:
                            self._mono = None
                    return
                local_path = temp_path

            sf_error: Exception | None = None
            data = sr = None
            try:
                data, sr = sf.read(local_path, always_2d=True, dtype="float32")
            except Exception as exc:  # noqa: BLE001 — try the audioread fallback below first
                sf_error = exc

            if data is None:
                # `soundfile`/libsndfile can't decode AAC/M4A or Opus/
                # WebM — exactly what online-track downloads almost
                # always are (see module docstring) — so this fallback
                # is what actually makes the online/playlist spectrum
                # work at all, not just a rare-format nicety.
                if not _HAS_AUDIOREAD:
                    raise sf_error or RuntimeError("soundfile returned no data")
                try:
                    data, sr = _decode_with_audioread(local_path)
                except Exception as ar_exc:  # noqa: BLE001
                    logger.warning(
                        "Visualizer could not decode %s (soundfile: %s; audioread: %s)",
                        path, sf_error, ar_exc,
                    )
                    with self._lock:
                        if self._loading_path == path:
                            self._mono = None
                    return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Visualizer could not decode %s: %s", path, exc)
            with self._lock:
                if self._loading_path == path:
                    self._mono = None
            return
        finally:
            if temp_path is not None:
                Path(temp_path).unlink(missing_ok=True)

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
