"""
radio.py
========
"Play more songs like this" — builds a queue of tracks similar to a
seed (a specific track, an artist, or an album) without needing any
recommendation engine of our own.

The trick: YouTube already auto-generates a "Mix"/Radio playlist for
almost every video, reachable at

    https://www.youtube.com/watch?v=<ID>&list=RD<ID>

— the same "Radio" button YouTube's own UI shows next to a video. That
mix is a genuinely curated "more like this" playlist, so this module
just needs to find a seed video id and let yt-dlp list that mix, the
same flat-extraction technique `online_source.py` already uses for
ordinary playlists (see `resolve_playlist`). Metadata cleanup reuses
`online_source.enrich_with_itunes`, same as every other online-sourced
track in this app.

Two ways in:
  - `similar_tracks(seed_track=...)` — from a specific track (local or
    online). This is what powers Autoplay (`player.py`'s
    `on_queue_end`, wired up in `ui.py`) once the queue naturally
    runs out.
  - `similar_tracks(artist=..., album=...)` — from freeform text, for
    a user-initiated "radio ARTIST"/"radio ARTIST ALBUM" command.

Either way this only returns *listings* (`OnlineSearchResult`s, not
yet resolved to a playable stream) — same two-phase split as the rest
of online_source.py, so callers can show/queue results before paying
the cost of resolving every one of them (see
`online_source.resolve_playlist_bulk`).
"""

from __future__ import annotations

import logging
import re

import online_source
from metadata import TrackMetadata
from online_source import OnlineSearchResult

logger = logging.getLogger(__name__)


class RadioError(Exception):
    """Raised when a radio queue can't be built — no seed could be
    resolved to a YouTube video, or YouTube has no mix for it, or the
    lookup itself failed (network, yt-dlp missing, etc.). The message
    is meant to be shown to the user as-is, same contract as
    `OnlineSourceError`.
    """


# Matches an 11-character YouTube video id out of a watch/short/share
# URL, whichever form it's in.
_YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")

# How many extra mix entries to ask for beyond what the caller wants —
# the seed video itself and anything in `exclude_ids` get filtered out
# afterward, so asking for exactly `limit` would often under-deliver.
_MIX_OVERFETCH = 8

# YouTube typically caps a "Mix" listing around this many videos —
# asking for more than yt-dlp will ever actually return just wastes a
# slightly larger flat-extraction response.
_MIX_MAX_LIMIT = 50


def _extract_youtube_id(url: str | None) -> str | None:
    if not url:
        return None
    match = _YOUTUBE_ID_RE.search(url)
    return match.group(1) if match else None


def _seed_query_and_id(
    seed_track: TrackMetadata | None,
    artist: str | None,
    album: str | None,
    query: str | None,
) -> tuple[str | None, str]:
    """Figures out (a) a YouTube video id to seed the mix from, if one
    is already known for free, and (b) a human-readable query to fall
    back on (via `online_source.find_video_id`) when it isn't. Raises
    `RadioError` if the caller gave us nothing at all to go on.
    """
    if seed_track is not None:
        video_id = _extract_youtube_id(seed_track.source_url) if seed_track.is_online else None
        text = f"{seed_track.artist} {seed_track.title}".strip() or seed_track.title
        return video_id, text
    if query and query.strip():
        return None, query.strip()
    if artist and album:
        return None, f"{artist.strip()} {album.strip()}".strip()
    if artist and artist.strip():
        return None, artist.strip()
    if album and album.strip():
        return None, album.strip()
    raise RadioError("Radio needs a track, artist, or album to start from.")


def _fetch_mix_playlist(video_id: str, limit: int) -> list[OnlineSearchResult]:
    yt_dlp = online_source.get_yt_dlp()
    url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    opts = online_source.yt_dlp_opts(extract_flat="in_playlist", playlistend=limit)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 — yt-dlp raises many different exception types
        raise RadioError(f"Could not build a radio mix: {exc}") from exc

    entries = info.get("entries") if isinstance(info, dict) else None
    if not entries:
        raise RadioError("YouTube has no radio mix available for this track.")

    results: list[OnlineSearchResult] = []
    for entry in entries[:limit]:
        if not entry:
            continue
        entry_id = entry.get("id")
        if not entry_id:
            continue
        webpage_url = entry.get("url") or f"https://www.youtube.com/watch?v={entry_id}"
        artist, title = online_source.split_artist_title(
            entry.get("title") or "",
            entry.get("uploader") or entry.get("channel") or "Unknown Artist",
        )
        results.append(OnlineSearchResult(
            id=entry_id,
            title=title,
            artist=artist,
            album="Radio",
            duration=float(entry.get("duration") or 0.0),
            cover_url=online_source.thumbnail_url(entry, entry_id),
            source="youtube",
            webpage_url=webpage_url,
        ))
    return results


def similar_tracks(
    seed_track: TrackMetadata | None = None,
    artist: str | None = None,
    album: str | None = None,
    query: str | None = None,
    limit: int = 20,
    exclude_ids: set[str] | None = None,
) -> list[OnlineSearchResult]:
    """Returns up to `limit` tracks similar to the given seed — a
    specific track (`seed_track`), an artist, an album, or a raw
    `query` string. Exactly one seed kind should be meaningfully set;
    if more than one is given, `seed_track` wins, then `query`, then
    `artist`/`album`.

    `exclude_ids` (YouTube video ids) lets a caller keep a running
    "already played this session" set so a long Autoplay/radio session
    doesn't loop back over the same handful of tracks — entirely
    optional, an empty radio result just means "nothing new found".

    Raises `RadioError` on anything that stops a mix from being built:
    no seed could be resolved to a video, YouTube has no mix for it,
    or the network/yt-dlp lookup itself failed. Never returns an empty
    list silently — that case also raises, so callers don't need to
    special-case "worked, but found nothing" separately from "failed".
    """
    limit = max(1, min(limit, _MIX_MAX_LIMIT))
    video_id, seed_query = _seed_query_and_id(seed_track, artist, album, query)

    if video_id is None:
        video_id = online_source.find_video_id(seed_query)
    if video_id is None:
        raise RadioError(f"Couldn't find a seed track for '{seed_query}' to build a radio from.")

    entries = _fetch_mix_playlist(video_id, limit + _MIX_OVERFETCH)

    exclude = set(exclude_ids or ())
    exclude.add(video_id)
    results = [entry for entry in entries if entry.id not in exclude][:limit]
    if not results:
        raise RadioError(f"No similar tracks found for '{seed_query}'.")

    # Best-effort metadata cleanup, same as an ordinary playlist import
    # (see online_source.resolve_playlist) — never raises, a track
    # just keeps its YouTube-derived title/artist/cover if iTunes has
    # nothing for it.
    online_source.enrich_with_itunes(results)
    return results
