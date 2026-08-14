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
import re
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

import session_cleanup
from constants import COVERS_DIR
from metadata import TrackMetadata
from utils import text_hash

logger = logging.getLogger(__name__)

_ITUNES_SEARCH_URL = "https://itunes.apple.com/search"

# yt-dlp defaults to negotiating with YouTube's "web" player client,
# which means fetching and parsing the full webpage plus deciphering
# a signature cipher for every single extraction — the dominant cost
# in both `_youtube_fallback_search` and `resolve()`. Asking for the
# "android" client first skips almost all of that (no cipher, a much
# smaller response) and falls back to "web" automatically if YouTube
# ever rejects it for a given video, so this is a straight speed win
# with no loss of coverage. Applied to every yt-dlp call in this
# module via `_yt_dlp_opts()`.
_YT_DLP_FAST_EXTRACTOR_ARGS = {"youtube": {"player_client": ["android", "web"]}}


def _yt_dlp_opts(**overrides) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 8,
        "extractor_args": _YT_DLP_FAST_EXTRACTOR_ARGS,
        # No point spending time fetching things we never use.
        "writesubtitles": False,
        "writeautomaticsub": False,
        "writeinfojson": False,
    }
    opts.update(overrides)
    return opts


# How many playlist tracks get their audio resolved at once. Each
# resolve is one blocking network round-trip; running a handful in
# parallel (instead of one-at-a-time) is what makes "load this whole
# playlist" take seconds instead of minutes. Kept modest so a big
# playlist doesn't open dozens of sockets at once. Bumped from the
# original 5 -> 8: yt-dlp's per-resolve cost is dominated by network
# round-trip latency, not local CPU, so a few more concurrent workers
# is close to a free win for "how long does loading a playlist take"
# without meaningfully increasing how many sockets are open at once.
_PLAYLIST_RESOLVE_WORKERS = 8

# iTunes metadata cleanup (`_enrich_playlist_with_itunes`) is a much
# lighter request than an audio resolve — one small JSON GET, no
# yt-dlp extraction — so it can safely run with more concurrency than
# the audio-resolve pool above without the same "too many sockets"
# concern, which is what actually shortens "load this playlist"'s
# wall-clock time (the two passes are otherwise back-to-back; see
# `resolve_playlist`'s docstring).
_ITUNES_ENRICH_WORKERS = 12


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

# In-memory cache for iTunes lookups, keyed by (query, limit). The
# same (artist, title) pair gets looked up repeatedly within a single
# run — a track that shows up in more than one YouTube "Mix"/radio
# playlist (see radio.py), a playlist re-imported, or the same song
# turning up in back-to-back searches — and every one of those is
# otherwise a fresh network round-trip for metadata that hasn't
# changed. Session-lifetime only (never written to disk); capped so a
# very long-running session can't grow this unboundedly.
_itunes_cache_lock = threading.Lock()
_itunes_cache: dict[tuple[str, int], list[dict]] = {}
_ITUNES_CACHE_MAX_ENTRIES = 512


def _itunes_search(query: str, limit: int) -> list[dict]:
    cache_key = (query.strip().lower(), limit)
    with _itunes_cache_lock:
        cached = _itunes_cache.get(cache_key)
    if cached is not None:
        return cached

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
    results = payload.get("results") or []

    with _itunes_cache_lock:
        # Simple full-clear eviction rather than proper LRU bookkeeping
        # — this cache only exists to skip *duplicate* lookups within
        # a run, not to be a general-purpose store, so losing every
        # entry the one time the cap is hit just means a few queries
        # go back to a fresh network call instead of a wrong answer.
        if len(_itunes_cache) >= _ITUNES_CACHE_MAX_ENTRIES:
            _itunes_cache.clear()
        _itunes_cache[cache_key] = results
    return results


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

# Matches upload-noise like "(Official Video)", "[Official Lyric Video]",
# "(Audio)", "[HD]" — common on YouTube music uploads but useless (and
# actively harmful to matching) as part of a song title.
_TITLE_NOISE_RE = re.compile(
    r"\s*[\(\[][^()\[\]]*?"
    r"(official|lyrics?|audio|video|mv|hd|4k|visualizer|full\s*song|lyric\s*video)"
    r"[^()\[\]]*?[\)\]]\s*",
    re.IGNORECASE,
)


def _clean_video_title(raw: str) -> str:
    cleaned = _TITLE_NOISE_RE.sub(" ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or raw


def _split_artist_title(raw_title: str, fallback_artist: str) -> tuple[str, str]:
    """YouTube music uploads overwhelmingly title videos "Artist - Song"
    — parsing that gives a real performer name for lyric lookups
    (`lyrics_manager.py` searches lrclib by title+artist). The
    uploader/channel name alone is usually a label, aggregator, or
    "Various Artists"-style channel, not the actual artist, so lyric
    search against it rarely matches anything. Falls back to
    `fallback_artist` (the channel) when the title doesn't look like
    "Artist - Song".
    """
    cleaned = _clean_video_title(raw_title)
    if " - " in cleaned:
        artist, _, song = cleaned.partition(" - ")
        artist, song = artist.strip(), song.strip()
        if artist and song:
            return artist, song
    return fallback_artist, cleaned


def _thumbnail_url(entry: dict, video_id: str) -> str | None:
    """Flat yt-dlp extraction (`extract_flat`, used by both the
    playlist and non-playlist search paths below) frequently omits
    `thumbnail` entirely — full per-video extraction would fetch it,
    but that's exactly the expensive step flat extraction exists to
    skip. YouTube thumbnails live at a predictable URL keyed only by
    video ID, though, so this is used unconditionally instead of
    trusting the (usually-missing) field — covers were silently not
    downloading for most search/playlist results before this.
    """
    return entry.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


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
    opts = _yt_dlp_opts(
        extract_flat="in_playlist",
        default_search=f"ytsearch{limit}",
        noplaylist=True,
    )
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
        artist, song_title = _split_artist_title(
            entry.get("title") or query,
            entry.get("uploader") or entry.get("channel") or "Unknown Artist",
        )
        results.append(OnlineSearchResult(
            id=video_id,
            title=song_title,
            artist=artist,
            album="YouTube",
            duration=float(entry.get("duration") or 0.0),
            cover_url=_thumbnail_url(entry, video_id),
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

    # Narrowing the format request (rather than the full "bestaudio/best"
    # ladder) means yt-dlp has a smaller format list to fetch and rank —
    # a small but free speedup stacked on top of the android client above.
    opts = _yt_dlp_opts(
        format="bestaudio[ext=m4a]/bestaudio/best",
        noplaylist=True,
    )
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
        session_cleanup.track(dest)
        return str(dest)
    try:
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            dest.write_bytes(resp.read())
        session_cleanup.track(dest)
        return str(dest)
    except Exception as exc:  # noqa: BLE001 — a missing cover shouldn't block playback
        logger.debug("Cover download failed for %s: %s", result_id, exc)
        return None


# -- YouTube playlist search + import -------------------------------------
#
# Separate from the single-track search above: `search_playlists()`
# finds whole playlists (not individual videos), and `resolve_playlist()`
# / `resolve_playlist_bulk()` turn a picked playlist into real, playable
# tracks — a genuine playlist import, not just several single-track
# searches stacked into the queue one at a time.

# YouTube's own "Playlist" results filter (the `sp` query param on a
# search-results URL) — same value the site itself sends when you tick
# "Playlist" in the search filters UI.
_YT_PLAYLIST_FILTER = "EgIQAw%3D%3D"


@dataclass(slots=True)
class OnlinePlaylistResult:
    """One playlist found by `search_playlists()` — lightweight, just
    enough to list and let the user pick. Its actual tracks are only
    fetched once picked, via `resolve_playlist()`.
    """
    id: str
    title: str
    owner: str
    track_count: int | None
    webpage_url: str


def search_playlists(query: str, limit: int = 8) -> list[OnlinePlaylistResult]:
    """Search YouTube for playlists (not individual videos) matching
    `query`. Flat extraction — one network round-trip, no per-playlist
    track listing yet (see `resolve_playlist` for that).
    """
    yt_dlp = _get_yt_dlp()
    limit = max(1, min(limit, 25))
    search_url = (
        f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
        f"&sp={_YT_PLAYLIST_FILTER}"
    )
    opts = _yt_dlp_opts(extract_flat=True, playlistend=limit)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
    except Exception as exc:  # noqa: BLE001
        raise OnlineSourceError(f"Playlist search failed: {exc}") from exc

    entries = info.get("entries") if isinstance(info, dict) else None
    if not entries:
        raise OnlineSourceError(f"No playlists found for '{query}'")

    results: list[OnlinePlaylistResult] = []
    for entry in entries:
        if not entry:
            continue
        playlist_id = entry.get("id")
        if not playlist_id:
            continue
        webpage_url = entry.get("url") or f"https://www.youtube.com/playlist?list={playlist_id}"
        results.append(OnlinePlaylistResult(
            id=playlist_id,
            title=entry.get("title") or query,
            owner=entry.get("uploader") or entry.get("channel") or "Unknown",
            track_count=entry.get("playlist_count"),
            webpage_url=webpage_url,
        ))
        if len(results) >= limit:
            break

    if not results:
        raise OnlineSourceError(f"No playlists found for '{query}'")
    return results


def _itunes_best_match(title: str, artist: str) -> dict | None:
    """Single best-effort iTunes lookup for one already-known
    title/artist pair. Same endpoint `search()` uses for single-track
    search, just asked for one result instead of a picker's worth —
    used by `_enrich_playlist_with_itunes` below so playlist tracks get
    the same clean, accurately-tagged metadata a manually-searched
    track does, instead of whatever's parseable out of a YouTube video
    title. Returns None (never raises) on no match/network failure —
    the caller already has usable YouTube-derived metadata to fall
    back to.
    """
    query = f"{artist} {title}".strip()
    if not query:
        return None
    try:
        items = _itunes_search(query, limit=1)
    except Exception as exc:  # noqa: BLE001 — treat as "no iTunes match", keep YouTube metadata
        logger.debug("iTunes enrich lookup failed for %r: %s", query, exc)
        return None
    return items[0] if items else None


def _enrich_playlist_with_itunes(entries: list[OnlineSearchResult]) -> None:
    """Best-effort metadata cleanup pass for a freshly-listed playlist:
    looks each entry's (YouTube-title-derived) artist/title up on
    iTunes, same as the single-track `search()` path already does, and
    replaces title/artist/album/cover with the clean iTunes values
    where a match is found. This is what makes playlist imports use
    the same iTunes metadata source as ordinary track search (see
    README's "Online search (iTunes metadata, YouTube audio)" section)
    instead of raw, often-noisy YouTube upload titles — and, as a
    side effect, gives `lyrics_manager.py`'s lrclib lookup a much
    better-matching title/artist to search with.

    Runs in parallel (bounded by `_PLAYLIST_RESOLVE_WORKERS`, same cap
    used for the audio-resolve pass below) since it's one network call
    per track. Mutates `entries` in place; an entry iTunes has nothing
    for is left with its original YouTube-derived metadata untouched —
    same "never dead-end" fallback philosophy as `search()`. Never
    raises: a slow/broken iTunes lookup should degrade to "no cleanup"
    for that one track, not fail the whole playlist import.
    """
    if not entries:
        return
    with ThreadPoolExecutor(max_workers=_ITUNES_ENRICH_WORKERS) as pool:
        futures = {
            pool.submit(_itunes_best_match, entry.title, entry.artist): entry
            for entry in entries
        }
        for future in as_completed(futures):
            entry = futures[future]
            try:
                match = future.result()
            except Exception:  # noqa: BLE001 — a worker error just means no cleanup for this entry
                continue
            if not match:
                continue
            entry.title = match.get("trackName") or entry.title
            entry.artist = match.get("artistName") or entry.artist
            entry.album = match.get("collectionName") or entry.album
            cover_url = match.get("artworkUrl100")
            if cover_url:
                entry.cover_url = cover_url.replace("100x100bb", "600x600bb")


def resolve_playlist(
    playlist: OnlinePlaylistResult, limit: int = 25
) -> tuple[str, list[OnlineSearchResult]]:
    """Flat listing of a picked playlist's tracks (title, per video —
    no audio stream resolution yet), with each track's title/artist/
    album/cover cleaned up against iTunes where a match is found (see
    `_enrich_playlist_with_itunes`) — the same metadata source
    single-track `search()` uses, rather than raw YouTube upload
    titles. Returns `(playlist_title, entries)`; `entries` are ordinary
    `OnlineSearchResult`s (source="youtube", webpage_url already set)
    so each can go straight into `resolve()` / `resolve_playlist_bulk()`
    without another search — the iTunes enrichment only touches
    display/lyrics-lookup metadata, never the YouTube identity used to
    actually resolve audio. Capped to `limit` tracks — a giant playlist
    imported wholesale would make "load this playlist" a multi-minute
    wait; the user can always search again for the rest.
    """
    yt_dlp = _get_yt_dlp()
    limit = max(1, min(limit, 200))
    opts = _yt_dlp_opts(extract_flat="in_playlist", playlistend=limit)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(playlist.webpage_url, download=False)
    except Exception as exc:  # noqa: BLE001
        raise OnlineSourceError(f"Could not open playlist '{playlist.title}': {exc}") from exc

    entries = info.get("entries") if isinstance(info, dict) else None
    if not entries:
        raise OnlineSourceError(f"Playlist '{playlist.title}' has no tracks")

    title = info.get("title") or playlist.title
    results: list[OnlineSearchResult] = []
    for entry in entries[:limit]:
        if not entry:
            continue
        video_id = entry.get("id")
        if not video_id:
            continue
        webpage_url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"
        artist, song_title = _split_artist_title(
            entry.get("title") or title,
            entry.get("uploader") or entry.get("channel") or playlist.owner,
        )
        results.append(OnlineSearchResult(
            id=video_id,
            title=song_title,
            artist=artist,
            album=title,
            duration=float(entry.get("duration") or 0.0),
            cover_url=_thumbnail_url(entry, video_id),
            source="youtube",
            webpage_url=webpage_url,
        ))

    if not results:
        raise OnlineSourceError(f"Playlist '{playlist.title}' has no playable tracks")

    # Clean up metadata against iTunes before handing back — see
    # `_enrich_playlist_with_itunes` docstring. Best-effort: a track
    # keeps its YouTube-derived title/artist/cover if iTunes has
    # nothing for it, same as `search()`'s own fallback behaviour.
    _enrich_playlist_with_itunes(results)

    return title, results


def resolve_playlist_bulk(
    entries: list[OnlineSearchResult],
    progress_cb: Callable[[int, int], None] | None = None,
) -> list[TrackMetadata]:
    """Resolves every entry's actual playable audio stream, several at
    once (see `_PLAYLIST_RESOLVE_WORKERS`), so importing an N-track
    playlist takes roughly N/workers resolves' worth of time instead of
    N. A track that fails to resolve (region-locked, taken down, etc.)
    is skipped rather than failing the whole import — `progress_cb`,
    if given, is called as `(done, total)` after every attempt so a
    caller can show live "12/30" progress; order of completion isn't
    guaranteed to match `entries`' order.
    """
    total = len(entries)
    tracks: list[TrackMetadata] = []
    done = 0
    with ThreadPoolExecutor(max_workers=_PLAYLIST_RESOLVE_WORKERS) as pool:
        futures = {pool.submit(resolve, entry): entry for entry in entries}
        for future in as_completed(futures):
            entry = futures[future]
            try:
                tracks.append(future.result())
            except OnlineSourceError as exc:
                logger.debug("Skipping playlist track %r: %s", entry.title, exc)
            done += 1
            if progress_cb is not None:
                try:
                    progress_cb(done, total)
                except Exception:  # noqa: BLE001 — a UI callback must never break the import
                    logger.exception("progress_cb raised during playlist resolve")

    # `as_completed` yields in whatever order finished first — restore
    # the playlist's original track order for a sane queue afterward.
    order = {entry.webpage_url: i for i, entry in enumerate(entries)}
    tracks.sort(key=lambda t: order.get(t.source_url, 0))
    return tracks


# -- public helpers for radio.py ------------------------------------------
#
# radio.py needs the same yt-dlp plumbing this module already has
# (fast-client options, the yt-dlp import guard, title parsing,
# thumbnail fallback, iTunes enrichment) to build a "songs similar to
# X" queue — thin wrappers around the private helpers above rather
# than duplicating any of it, so there's exactly one place that knows
# how to talk to yt-dlp/iTunes.

def get_yt_dlp():
    """Public alias for `_get_yt_dlp()` — see its docstring."""
    return _get_yt_dlp()


def yt_dlp_opts(**overrides) -> dict:
    """Public alias for `_yt_dlp_opts()` — see its docstring."""
    return _yt_dlp_opts(**overrides)


def split_artist_title(raw_title: str, fallback_artist: str) -> tuple[str, str]:
    """Public alias for `_split_artist_title()` — see its docstring."""
    return _split_artist_title(raw_title, fallback_artist)


def thumbnail_url(entry: dict, video_id: str) -> str | None:
    """Public alias for `_thumbnail_url()` — see its docstring."""
    return _thumbnail_url(entry, video_id)


def enrich_with_itunes(entries: list[OnlineSearchResult]) -> None:
    """Public alias for `_enrich_playlist_with_itunes()` — see its docstring."""
    _enrich_playlist_with_itunes(entries)


def find_video_id(query: str) -> str | None:
    """Cheapest possible YouTube lookup: just the video id of the top
    result for `query`, nothing else resolved (no format/stream
    lookup). Used by radio.py to find a seed video for a text
    (artist/album) radio request, or for a local-library seed track
    that has no YouTube id of its own to start from. Flat search —
    same cost/shape as `_youtube_fallback_search`. Returns None
    (never raises) if nothing is found or the search itself fails;
    a missing seed is the caller's problem to report, not this
    function's to raise about.
    """
    try:
        results = _youtube_fallback_search(query, limit=1)
    except OnlineSourceError:
        return None
    return results[0].id if results else None
