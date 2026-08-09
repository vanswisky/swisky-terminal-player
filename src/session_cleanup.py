"""
session_cleanup.py
===================
Online mode writes three kinds of files to disk as a side effect of
searching/playing: cover art (`online_source._cache_cover`), lyrics
auto-fetched for an online track (`lyrics_manager.py`), and the
rendered ASCII cache entry for an online cover (`ascii_cache.py`).
None of that is reusable the way it is for local library tracks — a
YouTube stream URL is short-lived and the track probably won't be
searched for again — so left alone it just grows on disk forever.

This module is a tiny, dependency-free registry: anything written for
an *online* track calls `track()` with the path, and `cleanup()` -
called once on app exit - deletes everything that was registered
this run. Local-library covers/lyrics/ASCII renders are never passed
in here, so they're never touched.

Deletion is best-effort: a file that's already gone, permission
errors, etc. are logged and skipped, never raised — a cleanup problem
should never turn into a crash on exit.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_tracked: set[Path] = set()


def track(path: str | Path | None) -> None:
    """Register a file written for an online track so `cleanup()`
    removes it later. Safe to call with None (e.g. a cover that
    failed to download) — silently ignored.
    """
    if not path:
        return
    with _lock:
        _tracked.add(Path(path))


def cleanup() -> int:
    """Delete every tracked file. Returns how many were actually
    removed. Safe to call even if nothing was ever tracked (e.g.
    online mode was never used this run, or auto-cleanup is off).
    """
    with _lock:
        paths = list(_tracked)
        _tracked.clear()

    removed = 0
    for path in paths:
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            logger.debug("Could not remove online-mode cache file %s: %s", path, exc)

    if removed:
        logger.info("Cleaned up %d online-mode cache file(s) on exit", removed)
    return removed
