"""
metadata.py
===========
Extracts tag metadata (title, artist, album, genre, codec, bitrate,
sample rate, duration) and embedded cover art from audio files using
`mutagen`. Cover art is written once to `COVERS_DIR` and reused via
`utils.file_hash`, so re-scans don't re-extract unchanged files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.mp4 import MP4

from constants import COVERS_DIR
from utils import file_hash

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TrackMetadata:
    path: str
    title: str = "Unknown Title"
    artist: str = "Unknown Artist"
    album: str = "Unknown Album"
    genre: str = "Unknown"
    duration: float = 0.0
    codec: str = ""
    bitrate: int = 0          # kbps
    sample_rate: int = 0      # Hz
    cover_path: str | None = None
    date_added: float = 0.0
    # Set for tracks resolved via online_source.py instead of the local
    # library scanner. `path` for these is a (usually short-lived,
    # signed) direct stream URL rather than a filesystem path — code
    # that does filesystem operations on `path` (scanner, file_hash,
    # lyrics-by-file-stem lookup) must check this flag first.
    is_online: bool = False
    # The original webpage URL (e.g. the resolved YouTube watch page
    # that supplied the audio) — kept for
    # reference/debugging, never used for playback.
    source_url: str | None = None
    # A list of pre-formatted "Key: Value" entries mpv needs to fetch
    # the stream URL above (some CDNs 403 without a matching
    # User-Agent/Referer). None/empty for local files. Kept as a list
    # (not a joined string) because AudioEngine.load() sets this as a
    # native mpv list-option property — a joined string would collide
    # with mpv's own comma-separated header format once passed through
    # a per-file options string (see AudioEngine.load()'s docstring).
    stream_headers: list[str] | None = None


def _first(value):
    if isinstance(value, list) and value:
        return str(value[0])
    if value:
        return str(value)
    return None


def _extract_cover(audio, source_path: str) -> str | None:
    """Write embedded cover art to disk once per unique file, return its path."""
    key = file_hash(source_path)
    dest = COVERS_DIR / f"{key}.jpg"
    if dest.exists():
        return str(dest)

    data: bytes | None = None
    try:
        if isinstance(audio, MP4):
            covers = audio.tags.get("covr") if audio.tags else None
            if covers:
                data = bytes(covers[0])
        elif isinstance(audio, FLAC):
            if audio.pictures:
                data = audio.pictures[0].data
        else:
            # ID3-tagged formats (MP3, AIFF, etc.)
            tags = getattr(audio, "tags", None)
            if tags is None:
                return None
            for key_name in tags.keys():
                if key_name.startswith("APIC"):
                    data = tags[key_name].data
                    break
    except Exception as exc:  # noqa: BLE001 — tag parsing is inherently messy
        logger.debug("Cover extraction failed for %s: %s", source_path, exc)
        return None

    if not data:
        return None

    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return str(dest)


def read_metadata(path: str) -> TrackMetadata:
    """Read tags + embedded art for a single audio file. Never raises —
    on any parse failure, returns best-effort defaults so a single bad
    file doesn't break a library scan.
    """
    meta = TrackMetadata(path=path, title=Path(path).stem)
    try:
        audio = MutagenFile(path, easy=False)
        if audio is None:
            return meta

        tags = audio.tags
        if tags:
            def tag(*keys):
                for k in keys:
                    try:
                        if k in tags:
                            return _first(tags[k])
                    except Exception:  # noqa: BLE001
                        continue
                return None

            meta.title = (
                tag("TIT2", "\xa9nam", "TITLE", "Title") or meta.title
            )
            meta.artist = (
                tag("TPE1", "\xa9ART", "ARTIST", "Artist") or meta.artist
            )
            meta.album = (
                tag("TALB", "\xa9alb", "ALBUM", "Album") or meta.album
            )
            meta.genre = (
                tag("TCON", "\xa9gen", "GENRE", "Genre") or meta.genre
            )

        info = getattr(audio, "info", None)
        if info is not None:
            meta.duration = float(getattr(info, "length", 0.0) or 0.0)
            meta.bitrate = int(getattr(info, "bitrate", 0) or 0) // 1000
            meta.sample_rate = int(getattr(info, "sample_rate", 0) or 0)
            meta.codec = type(info).__name__.replace("Info", "").upper()

        meta.cover_path = _extract_cover(audio, path)

    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read metadata for %s: %s", path, exc)

    return meta
