"""
config.py
=========
Typed, serializable configuration for every user-tunable knob in the
app (ASCII quality, visualizer FPS, color mode, theme, playback
speed, volume step, lyrics behaviour...).

`AppConfig` is the single source of truth passed by reference into
subsystems at startup. `settings_manager.py` owns loading/saving it
to disk; this module only defines *shape* and *defaults*.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum


class AsciiQuality(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    ULTRA = "ultra"


class AsciiRenderMode(str, Enum):
    CLASSIC = "classic"
    BLOCK = "block"
    BRAILLE = "braille"


class ColorMode(str, Enum):
    TRUECOLOR = "truecolor"
    ANSI256 = "256"
    MONOCHROME = "monochrome"


class RepeatMode(str, Enum):
    OFF = "off"
    ONE = "one"
    ALL = "all"


# Character cell resolution (columns per quality tier). Rows are derived
# from the source image aspect ratio and terminal cell aspect (~2:1).
ASCII_QUALITY_COLUMNS: dict[AsciiQuality, int] = {
    AsciiQuality.LOW: 60,
    AsciiQuality.MEDIUM: 90,
    AsciiQuality.HIGH: 130,
    AsciiQuality.ULTRA: 180,
}


@dataclass(slots=True)
class AsciiConfig:
    quality: AsciiQuality = AsciiQuality.HIGH
    mode: AsciiRenderMode = AsciiRenderMode.BRAILLE
    color_mode: ColorMode = ColorMode.TRUECOLOR
    auto_density: bool = True  # let the renderer pick char density per-image


@dataclass(slots=True)
class VisualizerConfig:
    enabled: bool = True
    fps: int = 60
    band_count: int = 48
    smoothing: float = 0.65      # exponential smoothing factor [0..1)
    peak_hold_seconds: float = 0.8
    stereo: bool = True


@dataclass(slots=True)
class PlaybackConfig:
    speed: float = 1.0
    volume: int = 80
    volume_step: int = 5
    repeat: RepeatMode = RepeatMode.OFF
    shuffle: bool = False


@dataclass(slots=True)
class LyricsConfig:
    enabled: bool = True
    auto_scroll: bool = True
    offset_ms: int = 0
    # When no local .lrc is found, look one up on lrclib.net (a free,
    # open lyrics database — no API key needed) and cache it to
    # LYRICS_DIR for next time. Runs in a background thread so it
    # never blocks playback; see lyrics_manager.py.
    auto_fetch: bool = True


@dataclass(slots=True)
class OnlineConfig:
    enabled: bool = True
    # How many results to show for a single `online <query>` search.
    search_results: int = 8


@dataclass(slots=True)
class AppConfig:
    theme: str = "purple"
    ascii: AsciiConfig = field(default_factory=AsciiConfig)
    visualizer: VisualizerConfig = field(default_factory=VisualizerConfig)
    playback: PlaybackConfig = field(default_factory=PlaybackConfig)
    lyrics: LyricsConfig = field(default_factory=LyricsConfig)
    online: OnlineConfig = field(default_factory=OnlineConfig)
    ui_fps: int = 30
    mouse_enabled: bool = True
    library_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        # Enums -> plain values for JSON.
        d["ascii"]["quality"] = self.ascii.quality.value
        d["ascii"]["mode"] = self.ascii.mode.value
        d["ascii"]["color_mode"] = self.ascii.color_mode.value
        d["playback"]["repeat"] = self.playback.repeat.value
        return d

    @staticmethod
    def from_dict(d: dict) -> "AppConfig":
        cfg = AppConfig()
        cfg.theme = d.get("theme", cfg.theme)
        cfg.ui_fps = d.get("ui_fps", cfg.ui_fps)
        cfg.mouse_enabled = d.get("mouse_enabled", cfg.mouse_enabled)
        cfg.library_paths = d.get("library_paths", cfg.library_paths)

        a = d.get("ascii", {})
        cfg.ascii = AsciiConfig(
            quality=AsciiQuality(a.get("quality", cfg.ascii.quality.value)),
            mode=AsciiRenderMode(a.get("mode", cfg.ascii.mode.value)),
            color_mode=ColorMode(a.get("color_mode", cfg.ascii.color_mode.value)),
            auto_density=a.get("auto_density", cfg.ascii.auto_density),
        )

        v = d.get("visualizer", {})
        cfg.visualizer = VisualizerConfig(
            enabled=v.get("enabled", cfg.visualizer.enabled),
            fps=v.get("fps", cfg.visualizer.fps),
            band_count=v.get("band_count", cfg.visualizer.band_count),
            smoothing=v.get("smoothing", cfg.visualizer.smoothing),
            peak_hold_seconds=v.get("peak_hold_seconds", cfg.visualizer.peak_hold_seconds),
            stereo=v.get("stereo", cfg.visualizer.stereo),
        )

        p = d.get("playback", {})
        cfg.playback = PlaybackConfig(
            speed=p.get("speed", cfg.playback.speed),
            volume=p.get("volume", cfg.playback.volume),
            volume_step=p.get("volume_step", cfg.playback.volume_step),
            repeat=RepeatMode(p.get("repeat", cfg.playback.repeat.value)),
            shuffle=p.get("shuffle", cfg.playback.shuffle),
        )

        l = d.get("lyrics", {})
        cfg.lyrics = LyricsConfig(
            enabled=l.get("enabled", cfg.lyrics.enabled),
            auto_scroll=l.get("auto_scroll", cfg.lyrics.auto_scroll),
            offset_ms=l.get("offset_ms", cfg.lyrics.offset_ms),
            auto_fetch=l.get("auto_fetch", cfg.lyrics.auto_fetch),
        )

        o = d.get("online", {})
        cfg.online = OnlineConfig(
            enabled=o.get("enabled", cfg.online.enabled),
            search_results=o.get("search_results", cfg.online.search_results),
        )
        return cfg
