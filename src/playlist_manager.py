"""
playlist_manager.py
====================
Owns the full track library (or a named playlist subset): add,
remove, reorder, sort, search/filter, and save/load to JSON under
`assets/playlists/`.

This is a passive data structure — it doesn't know about playback
state. `player.py` asks it "what's next" via `queue_manager.py`.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from constants import PLAYLISTS_DIR
from metadata import TrackMetadata

logger = logging.getLogger(__name__)


class SortKey(str, Enum):
    NAME = "name"
    ARTIST = "artist"
    ALBUM = "album"
    DURATION = "duration"
    DATE_ADDED = "date_added"
    RANDOM = "random"


@dataclass(slots=True)
class Playlist:
    name: str
    tracks: list[TrackMetadata] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "tracks": [t.path for t in self.tracks],
        }


class PlaylistManager:
    def __init__(self) -> None:
        self._library: list[TrackMetadata] = []
        self._filtered: list[TrackMetadata] = []
        self._filter_query: str = ""
        self._playlists: dict[str, Playlist] = {}

    # -- library (the full scanned set) -----------------------------------

    def set_library(self, tracks: list[TrackMetadata]) -> None:
        self._library = tracks
        self._apply_filter()

    def add_track(self, track: TrackMetadata) -> None:
        if not any(t.path == track.path for t in self._library):
            self._library.append(track)
            self._apply_filter()

    def remove_track(self, path: str) -> None:
        self._library = [t for t in self._library if t.path != path]
        self._apply_filter()

    def library(self) -> list[TrackMetadata]:
        return list(self._library)

    # -- search / filter -----------------------------------------------

    def filter(self, query: str) -> list[TrackMetadata]:
        self._filter_query = query.lower().strip()
        self._apply_filter()
        return self.visible()

    def _apply_filter(self) -> None:
        if not self._filter_query:
            self._filtered = list(self._library)
            return
        q = self._filter_query
        self._filtered = [
            t for t in self._library
            if q in t.title.lower() or q in t.artist.lower() or q in t.album.lower()
        ]

    def visible(self) -> list[TrackMetadata]:
        return list(self._filtered)

    # -- sorting -----------------------------------------------------------

    def sort(self, key: SortKey, reverse: bool = False) -> None:
        if key == SortKey.RANDOM:
            random.shuffle(self._filtered)
            return
        keyfunc = {
            SortKey.NAME: lambda t: t.title.lower(),
            SortKey.ARTIST: lambda t: t.artist.lower(),
            SortKey.ALBUM: lambda t: t.album.lower(),
            SortKey.DURATION: lambda t: t.duration,
            SortKey.DATE_ADDED: lambda t: t.date_added,
        }[key]
        self._filtered.sort(key=keyfunc, reverse=reverse)

    # -- move -----------------------------------------------------------

    def move(self, from_index: int, to_index: int) -> None:
        if not (0 <= from_index < len(self._filtered)) or not (0 <= to_index < len(self._filtered)):
            return
        item = self._filtered.pop(from_index)
        self._filtered.insert(to_index, item)

    # -- named playlists (save/load) -------------------------------------

    def save_playlist(self, name: str, tracks: list[TrackMetadata]) -> None:
        pl = Playlist(name=name, tracks=list(tracks))
        self._playlists[name] = pl
        PLAYLISTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = PLAYLISTS_DIR / f"{name}.json"
        try:
            out_path.write_text(json.dumps(pl.to_dict(), indent=2), encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to save playlist %s: %s", name, exc)

    def load_playlist(self, name: str) -> list[TrackMetadata]:
        path = PLAYLISTS_DIR / f"{name}.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            paths = set(data.get("tracks", []))
            return [t for t in self._library if t.path in paths]
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Failed to load playlist %s: %s", name, exc)
            return []

    def list_saved_playlists(self) -> list[str]:
        PLAYLISTS_DIR.mkdir(parents=True, exist_ok=True)
        return [p.stem for p in PLAYLISTS_DIR.glob("*.json")]
