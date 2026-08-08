"""
scanner.py
==========
Recursively scans configured music folders for supported audio files,
extracts metadata via `metadata.py`, and watches those folders with
`watchdog` so newly added/removed/edited files update the library
live without restarting the app.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from constants import SUPPORTED_EXTENSIONS
from metadata import TrackMetadata, read_metadata

logger = logging.getLogger(__name__)

LibraryChangeCallback = Callable[[], None]


def scan_directory(root: str) -> list[TrackMetadata]:
    """One-shot recursive scan. Never raises on individual bad files."""
    results: list[TrackMetadata] = []
    root_path = Path(root)
    if not root_path.exists():
        logger.warning("Music directory does not exist: %s", root)
        return results

    for path in root_path.rglob("*"):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            try:
                meta = read_metadata(str(path))
                meta.date_added = path.stat().st_mtime
                results.append(meta)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to read %s during scan", path)
    return results


class _DebouncedHandler(FileSystemEventHandler):
    """Coalesces bursts of filesystem events (e.g. a large copy) into a
    single rescan after a short quiet period, rather than rescanning per-file.
    """

    def __init__(self, on_change: LibraryChangeCallback, debounce_seconds: float = 1.5) -> None:
        self._on_change = on_change
        self._debounce = debounce_seconds
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _schedule(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._on_change)
            self._timer.daemon = True
            self._timer.start()

    def _is_relevant(self, path: str) -> bool:
        return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS

    def on_created(self, event):  # noqa: ANN001
        if not event.is_directory and self._is_relevant(event.src_path):
            self._schedule()

    def on_deleted(self, event):  # noqa: ANN001
        if not event.is_directory and self._is_relevant(event.src_path):
            self._schedule()

    def on_modified(self, event):  # noqa: ANN001
        if not event.is_directory and self._is_relevant(event.src_path):
            self._schedule()

    def on_moved(self, event):  # noqa: ANN001
        self._schedule()


class LibraryScanner:
    """Owns one or more watched directories and re-scans on change."""

    def __init__(self, directories: list[str]) -> None:
        self._directories = directories
        self._observer = Observer()
        self._change_listeners: list[LibraryChangeCallback] = []
        self._handler = _DebouncedHandler(self._notify_change)
        self._started = False

    def on_change(self, callback: LibraryChangeCallback) -> None:
        self._change_listeners.append(callback)

    def _notify_change(self) -> None:
        for cb in list(self._change_listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001
                logger.exception("Library change callback raised")

    def scan_all(self) -> list[TrackMetadata]:
        tracks: list[TrackMetadata] = []
        for directory in self._directories:
            tracks.extend(scan_directory(directory))
        return tracks

    def start_watching(self) -> None:
        if self._started:
            return
        for directory in self._directories:
            if Path(directory).exists():
                self._observer.schedule(self._handler, directory, recursive=True)
        self._observer.daemon = True
        self._observer.start()
        self._started = True

    def stop_watching(self) -> None:
        if self._started:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._started = False
