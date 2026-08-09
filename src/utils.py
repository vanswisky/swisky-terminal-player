"""
utils.py
========
Small, dependency-light helper functions shared across modules:
time formatting, safe clamping, hashing for cache keys, and terminal
size probing. Nothing here should import other app modules, to avoid
circular imports — this sits at the bottom of the dependency graph.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil

_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(text: str, max_len: int = 150) -> str:
    """Strip characters that aren't safe in a filename (path
    separators, control chars, Windows-reserved punctuation) so
    arbitrary user/online-sourced text — a playlist title, a track
    name — can be used as a save file's name without escaping the
    intended directory or failing outright on some filesystems.
    """
    cleaned = _UNSAFE_FILENAME_RE.sub("_", text).strip()
    return cleaned[:max_len] or "untitled"


def format_time(seconds: float) -> str:
    """Format seconds as `M:SS` or `H:MM:SS` for long tracks."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def file_hash(path: str, block_size: int = 65536) -> str:
    """Fast content hash used for ASCII-art / metadata cache keys.

    Uses size + mtime as a cheap fingerprint rather than hashing full
    file contents (music files and cover art can be large), falling
    back to a full hash only if stat() fails.

    Only for real filesystem paths — for anything else (URLs, search
    queries), use `text_hash` instead; calling this on a URL raises,
    since neither `os.stat` nor the plain `open()` fallback can reach
    the network.
    """
    try:
        stat = os.stat(path)
        fingerprint = f"{path}:{stat.st_size}:{stat.st_mtime_ns}"
        return hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:16]
    except OSError:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(block_size), b""):
                h.update(chunk)
        return h.hexdigest()[:16]


def text_hash(text: str) -> str:
    """Cache-key hash for arbitrary text (a URL, a video ID, a search
    query) rather than a filesystem path. Safe to call on anything —
    unlike `file_hash`, never touches the filesystem or network.
    """
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def terminal_size(default_cols: int = 100, default_rows: int = 32) -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=(default_cols, default_rows))
    return size.columns, size.lines


def ensure_dirs(*paths) -> None:
    for p in paths:
        os.makedirs(p, exist_ok=True)
