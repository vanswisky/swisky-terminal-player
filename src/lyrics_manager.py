"""
lyrics_manager.py
==================
Loads and parses `.lrc` synchronized lyrics for the current track,
and answers "which line is active right now" given a playback
position. Lyrics are matched by filename: `<track-stem>.lrc` inside
`LYRICS_DIR`, falling back to a same-named `.lrc` next to the audio
file itself.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from constants import LYRICS_DIR
from metadata import TrackMetadata

logger = logging.getLogger(__name__)

_LRC_TIME_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")


@dataclass(slots=True)
class LyricLine:
    time: float  # seconds
    text: str


@dataclass(slots=True)
class LyricsState:
    lines: list[LyricLine] = field(default_factory=list)
    available: bool = False
    offset_ms: int = 0

    def active_index(self, position_seconds: float) -> int:
        """Index of the currently active line, or -1 if before the first line."""
        if not self.lines:
            return -1
        adjusted = position_seconds - (self.offset_ms / 1000.0)
        lo, hi = 0, len(self.lines) - 1
        result = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.lines[mid].time <= adjusted:
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return result


def parse_lrc(text: str) -> list[LyricLine]:
    lines: list[LyricLine] = []
    for raw_line in text.splitlines():
        matches = list(_LRC_TIME_RE.finditer(raw_line))
        if not matches:
            continue
        content = _LRC_TIME_RE.sub("", raw_line).strip()
        for m in matches:
            minutes = int(m.group(1))
            seconds = int(m.group(2))
            frac = m.group(3)
            fraction = 0.0
            if frac:
                fraction = int(frac) / (1000 if len(frac) == 3 else 100)
            timestamp = minutes * 60 + seconds + fraction
            lines.append(LyricLine(time=timestamp, text=content))
    lines.sort(key=lambda l: l.time)
    return lines


class LyricsManager:
    def __init__(self, offset_ms: int = 0) -> None:
        self._state = LyricsState(offset_ms=offset_ms)
        self._current_track: TrackMetadata | None = None

    def load_for_track(self, track: TrackMetadata | None) -> LyricsState:
        self._current_track = track
        if track is None:
            self._state = LyricsState(offset_ms=self._state.offset_ms)
            return self._state

        candidates = [
            LYRICS_DIR / f"{Path(track.path).stem}.lrc",
            Path(track.path).with_suffix(".lrc"),
        ]
        for candidate in candidates:
            if candidate.exists():
                try:
                    text = candidate.read_text(encoding="utf-8", errors="ignore")
                    parsed = parse_lrc(text)
                    self._state = LyricsState(
                        lines=parsed, available=bool(parsed), offset_ms=self._state.offset_ms
                    )
                    return self._state
                except OSError as exc:
                    logger.warning("Failed to read lyrics %s: %s", candidate, exc)

        self._state = LyricsState(offset_ms=self._state.offset_ms)
        return self._state

    def reload(self) -> LyricsState:
        return self.load_for_track(self._current_track)

    def set_offset(self, offset_ms: int) -> None:
        self._state.offset_ms = offset_ms

    def adjust_offset(self, delta_ms: int) -> None:
        self._state.offset_ms += delta_ms

    def state(self) -> LyricsState:
        return self._state
