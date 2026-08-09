"""
lyrics_manager.py
==================
Loads and parses `.lrc` synchronized lyrics for the current track,
and answers "which line is active right now" given a playback
position. Lyrics are matched by filename: `<track-stem>.lrc` inside
`LYRICS_DIR`, falling back to a same-named `.lrc` next to the audio
file itself (local tracks only — online tracks have no meaningful
local path, see `_lyrics_cache_path`).

If nothing local is found and `auto_fetch` is enabled, a background
thread looks the track up on lrclib.net (a free, open lyrics database
— no API key needed) and caches whatever it finds to LYRICS_DIR, so
it's a plain local hit next time. The network call never blocks
playback: `load_for_track` returns immediately with "no lyrics yet",
and the fetch thread updates `state()` in place once (if) it lands —
`ui.py` re-reads `state()` every frame, so the lyrics panel just
starts showing them on whatever frame that happens to be.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import session_cleanup
from constants import LYRICS_DIR
from metadata import TrackMetadata
from utils import safe_filename

logger = logging.getLogger(__name__)

_LRC_TIME_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")
_LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"


@dataclass(slots=True)
class LyricLine:
    time: float  # seconds
    text: str


@dataclass(slots=True)
class LyricsState:
    lines: list[LyricLine] = field(default_factory=list)
    available: bool = False
    offset_ms: int = 0
    # True while a background lrclib lookup for the current track is
    # in flight — lets the lyrics panel show "Searching online..."
    # instead of a bare "No lyrics" while it waits.
    fetching: bool = False

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


def _sanitize_filename(text: str) -> str:
    return safe_filename(text)


def _fetch_synced_lyrics_lrclib(title: str, artist: str) -> str | None:
    """Blocking network call — only ever run from a background thread.
    Returns raw LRC-format text for the first result with time-synced
    lyrics, or None if lrclib has nothing usable (or is unreachable).
    """
    params = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
    url = f"{_LRCLIB_SEARCH_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "swisky-terminal-player/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            results = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — network/JSON errors are all "no lyrics found"
        logger.debug("lrclib lookup failed for %r - %r: %s", artist, title, exc)
        return None
    for entry in results or []:
        synced = entry.get("syncedLyrics")
        if synced:
            return synced
    return None


class LyricsManager:
    def __init__(self, offset_ms: int = 0, auto_fetch: bool = True) -> None:
        self._state = LyricsState(offset_ms=offset_ms)
        self._current_track: TrackMetadata | None = None
        self.auto_fetch = auto_fetch
        # Bumped every time load_for_track() is called; a background
        # fetch checks its captured generation before writing state,
        # so a slow lrclib response for a track the user has since
        # skipped past can't clobber whatever's playing now.
        self._fetch_gate = threading.Lock()
        self._fetch_generation = 0

    def _lyrics_cache_path(self, track: TrackMetadata) -> Path:
        """Where a local .lrc for this track lives (or would be cached
        to, after an auto-fetch). Online tracks have no meaningful
        local file stem — their `path` is a short-lived stream URL —
        so they're keyed by a sanitized "artist - title" instead.
        """
        if track.is_online:
            key = _sanitize_filename(f"{track.artist} - {track.title}")
        else:
            key = Path(track.path).stem
        return LYRICS_DIR / f"{key}.lrc"

    def load_for_track(self, track: TrackMetadata | None) -> LyricsState:
        self._current_track = track
        with self._fetch_gate:
            self._fetch_generation += 1
            generation = self._fetch_generation

        if track is None:
            self._state = LyricsState(offset_ms=self._state.offset_ms)
            return self._state

        candidates = [self._lyrics_cache_path(track)]
        if not track.is_online:
            candidates.append(Path(track.path).with_suffix(".lrc"))

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

        fetching = bool(self.auto_fetch and track.title and track.artist)
        self._state = LyricsState(offset_ms=self._state.offset_ms, fetching=fetching)
        if fetching:
            threading.Thread(
                target=self._fetch_worker, args=(track, generation), daemon=True
            ).start()
        return self._state

    def _fetch_worker(self, track: TrackMetadata, generation: int) -> None:
        synced = _fetch_synced_lyrics_lrclib(track.title, track.artist)

        with self._fetch_gate:
            stale = generation != self._fetch_generation
        if stale:
            return  # user already skipped to a different track

        if not synced:
            with self._fetch_gate:
                if generation == self._fetch_generation:
                    self._state = LyricsState(offset_ms=self._state.offset_ms, fetching=False)
            return

        cache_path = self._lyrics_cache_path(track)
        try:
            LYRICS_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(synced, encoding="utf-8")
            # Only online tracks' lyric files are session-cleanup
            # candidates — this same fetch path also runs for local
            # library tracks with no bundled .lrc, and those are worth
            # keeping around for next time they're played.
            if track.is_online:
                session_cleanup.track(cache_path)
        except OSError as exc:
            logger.warning("Failed to cache fetched lyrics to %s: %s", cache_path, exc)

        parsed = parse_lrc(synced)
        with self._fetch_gate:
            if generation != self._fetch_generation:
                return
            self._state = LyricsState(
                lines=parsed, available=bool(parsed), offset_ms=self._state.offset_ms
            )

    def reload(self) -> LyricsState:
        return self.load_for_track(self._current_track)

    def set_offset(self, offset_ms: int) -> None:
        self._state.offset_ms = offset_ms

    def adjust_offset(self, delta_ms: int) -> None:
        self._state.offset_ms += delta_ms

    def state(self) -> LyricsState:
        return self._state
