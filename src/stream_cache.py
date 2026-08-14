"""
stream_cache.py
================
Background prefetcher for online (YouTube-resolved) tracks. While the
current track plays, `StreamPrefetcher` downloads the *next* queued
track's audio stream into a local disk cache file ahead of time — so
by the time playback actually advances to it, there's nothing left to
fetch: no fresh connection, no exposure to a connection that happens
to be slow right at that moment. `ui.py` wires this in via
`Player.set_path_resolver`, checked once per track load.

Local library tracks are never touched — they're already instant,
reading straight off disk, with no network involved.

Only ever swaps in the cached copy once the download has *fully*
completed (see `_download`'s `completed` bookkeeping) — never a
partial file, which would otherwise just mean "playback stops early".
An interrupted/oversized download is treated as "no local shortcut for
this one", not an error: the track still plays fine straight from its
original stream URL, exactly as it would without prefetching at all.

Downloaded files live under `CACHE_DIR/prefetch/` and are registered
with `session_cleanup` (same auto-clean-on-exit contract as covers/
lyrics/ASCII renders written for online tracks — see
`session_cleanup.py`'s module docstring) so they never accumulate
across runs regardless of how many tracks a session prefetches.
"""

from __future__ import annotations

import logging
import threading
import urllib.request
from pathlib import Path

import session_cleanup
from constants import CACHE_DIR
from metadata import TrackMetadata
from utils import text_hash

logger = logging.getLogger(__name__)

PREFETCH_DIR = CACHE_DIR / "prefetch"

# Caps how much of one track's stream gets pulled down ahead of time.
# Comfortably above what a typical few-minutes-long compressed-audio
# track needs (a few MB), so almost every prefetch completes and
# becomes usable; a track that *doesn't* fit under this in one pass
# just falls back to streaming from its original URL like normal —
# see the `completed` check in `_download`.
_MAX_PREFETCH_BYTES = 40 * 1024 * 1024  # 40 MiB
_CHUNK_SIZE = 256 * 1024
_REQUEST_TIMEOUT_S = 10


class StreamPrefetcher:
    """One instance is shared for the whole app session (owned by
    `ui.App`). Thread-safe: `prefetch()` is called from the main
    thread each time the "next track" changes; the actual download
    runs on its own daemon thread and only ever touches shared state
    under `_lock`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_url: str | None = None
        self._ready: dict[str, str] = {}   # stream URL -> local cache path
        self._failed: set[str] = set()      # stream URLs not worth retrying this run

    @staticmethod
    def _cache_path(url: str) -> Path:
        return PREFETCH_DIR / f"{text_hash(url)}.audio"

    def local_path_for(self, track: TrackMetadata) -> str | None:
        """Returns a fully-downloaded local copy of `track`'s stream if
        one is ready, else `None` — in which case the caller should
        keep using `track.path` (the original local path or remote
        stream URL) unchanged. Safe to call for any track, online or
        not; always `None` for local-library tracks.
        """
        if not track.is_online:
            return None
        with self._lock:
            return self._ready.get(track.path)

    def prefetch(self, track: TrackMetadata | None) -> None:
        """Fire-and-forget: starts a background download of `track`'s
        stream into the local cache, unless one is already ready, in
        flight, or previously failed for this exact URL (cheap no-op
        in all three cases) — safe to call every time "what's next in
        the queue" changes, without needing to track state externally.
        Does nothing for `None` (nothing queued next) or a local
        library track (nothing to prefetch).
        """
        if track is None or not track.is_online:
            return
        url = track.path
        with self._lock:
            if url in self._ready or url in self._failed or url == self._active_url:
                return
            self._active_url = url
        threading.Thread(target=self._download, args=(track,), daemon=True).start()

    def _download(self, track: TrackMetadata) -> None:
        url = track.path
        dest = self._cache_path(url)
        tmp = dest.with_suffix(".part")
        completed = False
        try:
            PREFETCH_DIR.mkdir(parents=True, exist_ok=True)
            headers = {"User-Agent": "Mozilla/5.0"}
            # Some CDNs 403 without matching headers — same
            # `stream_headers` mpv itself is given for this track (see
            # AudioEngine.load()'s docstring for why online tracks
            # carry these at all).
            for entry in track.stream_headers or ():
                key, sep, value = entry.partition(":")
                if sep:
                    headers[key.strip()] = value.strip()

            req = urllib.request.Request(url, headers=headers)
            written = 0
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp, \
                    open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(_CHUNK_SIZE)
                    if not chunk:
                        completed = True  # genuine EOF from the server
                        break
                    f.write(chunk)
                    written += len(chunk)
                    if written >= _MAX_PREFETCH_BYTES:
                        break  # hit the cap before EOF — treat as incomplete, see below

            if completed:
                tmp.replace(dest)
                session_cleanup.track(dest)
                with self._lock:
                    self._ready[url] = str(dest)
            else:
                tmp.unlink(missing_ok=True)
                with self._lock:
                    self._failed.add(url)
        except Exception as exc:  # noqa: BLE001 — a failed prefetch just means "no local shortcut"; never fatal, never blocks playback
            logger.debug("Prefetch failed for %r: %s", track.title, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            with self._lock:
                self._failed.add(url)
        finally:
            with self._lock:
                if self._active_url == url:
                    self._active_url = None
