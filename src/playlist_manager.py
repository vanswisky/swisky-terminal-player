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
            "tracks": [_track_to_dict(t) for t in self.tracks],
        }


def _track_to_dict(t: TrackMetadata) -> dict:
    """Full metadata, not just `path` — the old format stored paths
    only and re-matched them against the scanned local library on
    load (see `PlaylistManager.load_playlist`'s old docstring note
    below), which meant a saved playlist could only ever contain
    local-library tracks: an *online* track's `path` is a stream URL
    that never appears in `self._library`, so any playlist saved from
    an online search or an imported online playlist (see
    `ui.py::_finish_playlist_import`, which calls `save_playlist`
    directly on online-resolved tracks) loaded back as silently empty
    — the exact case the "playlist" feature was actually missing a UI
    for. Storing the full track dict fixes that: loading no longer
    depends on the track still being present in whatever's currently
    scanned.
    """
    return {
        "path": t.path, "title": t.title, "artist": t.artist, "album": t.album,
        "genre": t.genre, "duration": t.duration, "codec": t.codec,
        "bitrate": t.bitrate, "sample_rate": t.sample_rate,
        "cover_path": t.cover_path, "date_added": t.date_added,
        "is_online": t.is_online, "source_url": t.source_url,
        "stream_headers": t.stream_headers,
    }


def _track_from_dict(d: dict) -> TrackMetadata:
    return TrackMetadata(
        path=d["path"],
        title=d.get("title", "Unknown Title"),
        artist=d.get("artist", "Unknown Artist"),
        album=d.get("album", "Unknown Album"),
        genre=d.get("genre", "Unknown"),
        duration=d.get("duration", 0.0),
        codec=d.get("codec", ""),
        bitrate=d.get("bitrate", 0),
        sample_rate=d.get("sample_rate", 0),
        cover_path=d.get("cover_path"),
        date_added=d.get("date_added", 0.0),
        is_online=d.get("is_online", False),
        source_url=d.get("source_url"),
        stream_headers=d.get("stream_headers"),
    )


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
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Failed to load playlist %s: %s", name, exc)
            return []

        raw_tracks = data.get("tracks", [])
        if raw_tracks and isinstance(raw_tracks[0], str):
            # Legacy format: a playlist saved before this stored full
            # metadata, back when only local-library tracks could
            # meaningfully be saved at all — re-matching against the
            # currently scanned library is still the correct lookup
            # for *these* files specifically (there's nothing else to
            # reconstruct an online track's metadata from). New saves
            # always use the dict format below (see `_track_to_dict`).
            paths = set(raw_tracks)
            return [t for t in self._library if t.path in paths]

        tracks = []
        for entry in raw_tracks:
            try:
                tracks.append(_track_from_dict(entry))
            except (KeyError, TypeError) as exc:
                logger.warning("Skipping malformed entry in playlist %s: %s", name, exc)
        return tracks

    def list_saved_playlists(self) -> list[str]:
        PLAYLISTS_DIR.mkdir(parents=True, exist_ok=True)
        return sorted(p.stem for p in PLAYLISTS_DIR.glob("*.json"))

    def count_saved_playlist(self, name: str) -> int:
        """Track count for one saved playlist without fully
        reconstructing every `TrackMetadata` — used by the playlist
        browser to show a count next to each name. Falls back to 0 for
        a corrupt/unreadable file rather than raising, since this is
        just a display hint.
        """
        path = PLAYLISTS_DIR / f"{name}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return len(data.get("tracks", []))
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            return 0

    def delete_playlist(self, name: str) -> bool:
        """Removes a saved playlist's JSON file (and its in-memory
        cache entry, if present). Returns whether a file actually
        existed to delete — lets the caller distinguish "deleted" from
        "there was nothing there" without needing a separate exists
        check first.
        """
        self._playlists.pop(name, None)
        path = PLAYLISTS_DIR / f"{name}.json"
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError as exc:
            logger.error("Failed to delete playlist %s: %s", name, exc)
            return False
