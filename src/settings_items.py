"""
settings_items.py
==================
Single source of truth for every row in the Settings screen: its
label, how to render its current value, and how LEFT/RIGHT change it.

Both `widgets.render_settings` (drawing) and `ui.py::_handle_settings_key`
(input) import `SETTINGS_ITEMS` instead of each keeping their own copy.
That duplication is exactly what used to make the Settings screen's
arrow keys silently do nothing: `widgets.py` knew how to *display* ten
settings, but `ui.py` never learned how to *change* any of them — the
key handler only understood ESC.

Adding a new setting only requires appending one `SettingItem` here;
both the display and the input side pick it up automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from config import AppConfig, AsciiQuality, AsciiRenderMode, ColorMode
from theme import THEMES

# -1 for LEFT, +1 for RIGHT.
Direction = int


@dataclass(slots=True)
class SettingItem:
    key: str
    label: str
    display: Callable[[AppConfig], str]
    change: Callable[[AppConfig, Direction], None]
    # Whether changing this setting means cached ASCII art is stale
    # and needs re-rendering.
    invalidates_cover: bool = False
    # Whether changing this setting means the active theme changed.
    changes_theme: bool = False


def _cycle_enum(enum_cls, current, direction: int):
    members = list(enum_cls)
    idx = (members.index(current) + direction) % len(members)
    return members[idx]


def _cycle_choice(choices: list[str], current: str, direction: int) -> str:
    if current not in choices:
        return choices[0]
    idx = (choices.index(current) + direction) % len(choices)
    return choices[idx]


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


SETTINGS_ITEMS: list[SettingItem] = [
    SettingItem(
        key="ascii_quality",
        label="ASCII Quality",
        display=lambda c: c.ascii.quality.value.upper(),
        change=lambda c, d: setattr(
            c.ascii, "quality", _cycle_enum(AsciiQuality, c.ascii.quality, d)
        ),
        invalidates_cover=True,
    ),
    SettingItem(
        key="ascii_mode",
        label="ASCII Renderer",
        display=lambda c: c.ascii.mode.value.upper(),
        change=lambda c, d: setattr(
            c.ascii, "mode", _cycle_enum(AsciiRenderMode, c.ascii.mode, d)
        ),
        invalidates_cover=True,
    ),
    SettingItem(
        key="color_mode",
        label="Color Mode",
        display=lambda c: c.ascii.color_mode.value.upper(),
        change=lambda c, d: setattr(
            c.ascii, "color_mode", _cycle_enum(ColorMode, c.ascii.color_mode, d)
        ),
        invalidates_cover=True,
    ),
    SettingItem(
        key="visualizer_enabled",
        label="Visualizer",
        display=lambda c: "ON" if c.visualizer.enabled else "OFF",
        change=lambda c, d: setattr(c.visualizer, "enabled", not c.visualizer.enabled),
    ),
    SettingItem(
        key="visualizer_fps",
        label="Visualizer FPS",
        display=lambda c: str(c.visualizer.fps),
        change=lambda c, d: setattr(
            c.visualizer, "fps", int(_clamp(c.visualizer.fps + d * 5, 10, 120))
        ),
    ),
    SettingItem(
        key="playback_speed",
        label="Playback Speed",
        display=lambda c: f"{c.playback.speed:.2f}x",
        change=lambda c, d: setattr(
            c.playback, "speed", round(_clamp(c.playback.speed + d * 0.05, 0.25, 3.0), 2)
        ),
    ),
    SettingItem(
        key="volume_step",
        label="Volume Step",
        display=lambda c: f"{c.playback.volume_step}%",
        change=lambda c, d: setattr(
            c.playback, "volume_step", int(_clamp(c.playback.volume_step + d, 1, 25))
        ),
    ),
    SettingItem(
        key="theme",
        label="Theme",
        display=lambda c: c.theme.upper(),
        change=lambda c, d: setattr(c, "theme", _cycle_choice(list(THEMES.keys()), c.theme, d)),
        changes_theme=True,
    ),
    SettingItem(
        key="lyrics_enabled",
        label="Lyrics",
        display=lambda c: "ON" if c.lyrics.enabled else "OFF",
        change=lambda c, d: setattr(c.lyrics, "enabled", not c.lyrics.enabled),
    ),
    SettingItem(
        key="lyrics_auto_scroll",
        label="Auto Scroll",
        display=lambda c: "ON" if c.lyrics.auto_scroll else "OFF",
        change=lambda c, d: setattr(c.lyrics, "auto_scroll", not c.lyrics.auto_scroll),
    ),
    SettingItem(
        key="lyrics_auto_fetch",
        label="Auto-fetch Lyrics",
        display=lambda c: "ON" if c.lyrics.auto_fetch else "OFF",
        change=lambda c, d: setattr(c.lyrics, "auto_fetch", not c.lyrics.auto_fetch),
    ),
    SettingItem(
        key="online_enabled",
        label="Online Search",
        display=lambda c: "ON" if c.online.enabled else "OFF",
        change=lambda c, d: setattr(c.online, "enabled", not c.online.enabled),
    ),
    SettingItem(
        key="online_results",
        label="Search Results",
        display=lambda c: str(c.online.search_results),
        change=lambda c, d: setattr(
            c.online, "search_results", int(_clamp(c.online.search_results + d, 3, 20))
        ),
    ),
    SettingItem(
        key="online_playlist_limit",
        label="Playlist Import Limit",
        display=lambda c: str(c.online.playlist_track_limit),
        change=lambda c, d: setattr(
            c.online, "playlist_track_limit",
            int(_clamp(c.online.playlist_track_limit + d * 5, 5, 100)),
        ),
    ),
    SettingItem(
        key="online_auto_cleanup",
        label="Auto-clean Online Cache",
        display=lambda c: "ON" if c.online.auto_cleanup else "OFF",
        change=lambda c, d: setattr(c.online, "auto_cleanup", not c.online.auto_cleanup),
    ),
]
