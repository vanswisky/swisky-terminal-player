"""
online_source.py
=================
Search and resolve playable tracks from a hybrid pipeline: metadata
from the iTunes Search API, audio from YouTube.

iTunes Search gives clean, accurately-tagged results (title, artist,
album, real cover art) with no API key, no account, and no rate-limit
paperwork — a straightforward JSON GET. It doesn't cover every obscure
or indie release, though, so if it comes back empty, `search()`
transparently falls back to a plain YouTube search instead (same
behaviour this module had before iTunes was added) so a query never
just dead-ends.

Either way, iTunes/YouTube-search results are never themselves
playable — this was also true when this module used to search Spotify
directly. Actual audio always comes from a YouTube stream, resolved
lazily in `resolve()`.

Two-phase by design, same shape as before and for the same reason:

  1. `search()` — cheap. One iTunes API call (falling back to one
     flat YouTube search if iTunes has nothing), returns just enough
     (title, artist, album, duration, cover URL) to show a picker.
  2. `resolve()` — only ever called once, for the single result the
     user actually picked.
       - For an iTunes-sourced result: looks up a matching video on
         YouTube for the actual audio (best-effort match on
         "<artist> <title>").
       - For a YouTube-fallback result: the video is already known
         (no extra search needed), so this just does the final full
         extraction for that video directly.
     Either way it grabs a real playable stream URL plus whatever HTTP
     headers mpv needs to fetch it, and caches the cover art into
     COVERS_DIR — so `ascii_cache.py` doesn't need to know or care
     that this track's metadata and its audio came from two different
     places.

Both phases raise `OnlineSourceError` on failure (no network, no
results, yt-dlp not installed, no matching video, etc.) so callers in
ui.py can show that message and move on instead of leaving the UI
stuck mid-search.

Network calls in this module are all synchronous/blocking — callers
are responsible for running them off the UI thread (see
`ui.py::_start_online_search` / `_resolve_and_play_online`).
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass

from constants import COVERS_DIR
from metadata import TrackMetadata
from utils import text_hash

logger = logging.getLogger(__name__)

_ITUNES_SEARCH_URL = "https://itunes.apple.com/search"


class OnlineSourceError(Exception):
    """Raised for anything that stops a search/resolve from completing
    — no network, no results, yt-dlp missing, no matching video, etc.
    The message is meant to be shown to the user as-is.
    """


@dataclass(slots=True)
class OnlineSearchResult:
    """Lightweight — just enough to list and let the user pick. Audio
    is only resolved for whichever one they select (see `resolve()`);
    resolving every result up front would mean a YouTube lookup per
    result just to draw a list.
    """
    id: str
    title: str
    artist: str
    album: str
    duration: float
    cover_url: str | None = None
    # "itunes" (resolve() needs a fresh YouTube lookup) or "youtube"
    # (this result *is* already a YouTube video — came from the
    # fallback search below — so resolve() can skip straight to
    # extracting it via `webpage_url`).
    source: str = "itunes"
    webpage_url: str | None = None  # set only when source == "youtube"


# -- iTunes (metadata search) --------------------------------------------

def _itunes_search(query: str, limit: int) -> list[dict]:
    params = urllib.parse.urlencode({
        "term": query,
        "media": "music",
        "entity": "song",
        "limit": limit,
    })
    req = urllib.request.Request(
        f"{_ITUNES_SEARCH_URL}?{params}",
        headers={"User-Agent": "swisky-terminal-player/1.0"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("results") or []


def search(query: str, limit: int = 8) -> list[OnlineSearchResult]:
    """Search iTunes' catalog for `query`, returning up to `limit`
    lightweight candidates. Falls back to a plain YouTube search if
    iTunes has nothing (common for obscure/indie tracks). Raises
    OnlineSourceError only if both sources come up empty/unreachable.
    """
    limit = max(1, min(limit, 25))

    try:
        items = _itunes_search(query, limit)
    except Exception as exc:  # noqa: BLE001 — treat as "iTunes had nothing", fall through
        logger.debug("iTunes search failed for %r: %s", query, exc)
        items = []

    results: list[OnlineSearchResult] = []
    for item in items:
        track_id = item.get("trackId")
        if not track_id:
            continue
        cover_url = item.get("artworkUrl100")
        if cover_url:
            # iTunes serves a small 100x100 thumbnail by default; ask
            # for the much sharper 600x600 version instead — same URL
            # scheme, just a different size token.
            cover_url = cover_url.replace("100x100bb", "600x600bb")
        results.append(OnlineSearchResult(
            id=str(track_id),
            title=item.get("trackName") or query,
            artist=item.get("artistName") or "Unknown Artist",
            album=item.get("collectionName") or "Unknown Album",
            duration=float(item.get("trackTimeMillis") or 0) / 1000.0,
            cover_url=cover_url,
            source="itunes",
        ))

    if results:
        return results

    return _youtube_fallback_search(query, limit)


# -- YouTube (fallback search + always-on audio resolution) --------------

def _get_yt_dlp():
    try:
        import yt_dlp
    except ImportError as exc:
        raise OnlineSourceError(
            "Online search/playback needs the 'yt-dlp' package. Install it with: pip install yt-dlp"
        ) from exc
    return yt_dlp


def _youtube_fallback_search(query: str, limit: int) -> list[OnlineSearchResult]:
    """Used only when iTunes returns nothing for `query`. Flat
    extraction, same cost/shape as this module's old plain-YouTube
    search: one network round-trip, no per-result stream resolution.
    """
    yt_dlp = _get_yt_dlp()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "default_search": f"ytsearch{limit}",
        "noplaylist": True,
        "socket_timeout": 10,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
    except Exception as exc:  # noqa: BLE001 — yt-dlp raises many different exception types
        raise OnlineSourceError(f"Search failed: {exc}") from exc

    entries = info.get("entries") if isinstance(info, dict) else None
    if not entries:
        raise OnlineSourceError(f"No results for '{query}'")

    results: list[OnlineSearchResult] = []
    for entry in entries:
        if not entry:
            continue
        video_id = entry.get("id")
        if not video_id:
            continue
        webpage_url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"
        results.append(OnlineSearchResult(
            id=video_id,
            title=entry.get("title") or query,
            artist=entry.get("uploader") or entry.get("channel") or "Unknown Artist",
            album="YouTube",
            duration=float(entry.get("duration") or 0.0),
            cover_url=entry.get("thumbnail"),
            source="youtube",
            webpage_url=webpage_url,
        ))

    if not results:
        raise OnlineSourceError(f"No results for '{query}'")
    return results


def resolve(result: OnlineSearchResult) -> TrackMetadata:
    """Full resolution for a single picked result: gets a real
    playable YouTube stream URL (searching for a match first if this
    result came from iTunes and doesn't already point at a specific
    video), plus any HTTP headers mpv needs to fetch it, and a
    locally-cached cover. This is the slow step — only call it once,
    for the track the user actually chose.
    """
    yt_dlp = _get_yt_dlp()
    target = result.webpage_url if (result.source == "youtube" and result.webpage_url) \
        else f"ytsearch1:{result.artist} - {result.title} audio"

    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 10,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as exc:  # noqa: BLE001
        raise OnlineSourceError(f"Could not find audio for '{result.title}': {exc}") from exc

    entries = info.get("entries") if isinstance(info, dict) else None
    match = (entries[0] if entries else info) or {}

    stream_url = match.get("url")
    if not stream_url:
        raise OnlineSourceError(f"No playable audio stream found for '{result.title}'")

    headers = match.get("http_headers") or {}
    header_list = [f"{k}: {v}" for k, v in headers.items()] or None

    cover_path = _cache_cover(result.cover_url, result.id)

    return TrackMetadata(
        path=stream_url,
        title=result.title,
        artist=result.artist,
        album=result.album,
        genre="Unknown",
        duration=result.duration or float(match.get("duration") or 0.0),
        codec=(match.get("acodec") or "").upper(),
        bitrate=int(match.get("abr") or 0),
        sample_rate=int(match.get("asr") or 0),
        cover_path=cover_path,
        is_online=True,
        source_url=match.get("webpage_url"),
        stream_headers=header_list,
    )


def _cache_cover(cover_url: str | None, result_id: str) -> str | None:
    if not cover_url:
        return None
    dest = COVERS_DIR / f"online-{text_hash(result_id)}.jpg"
    if dest.exists():
        return str(dest)
    try:
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            dest.write_bytes(resp.read())
        return str(dest)
    except Exception as exc:  # noqa: BLE001 — a missing cover shouldn't block playback
        logger.debug("Cover download failed for %s: %s", result_id, exc)
        return None
