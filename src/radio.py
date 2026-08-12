"""
radio.py
========
YouTube-Music-style "Radio" / autoplay: when the queue is close to
running out, `player.py` calls `RadioMixer.next_batch()` to pull a
handful of extra tracks from the *full* library (not just the current
queue), weighted toward artists/genres that have actually been
playing recently — so the mix drifts with listening taste instead of
sampling the whole library uniformly.

Deliberately pure selection logic: it takes plain lists in and
returns a list of `TrackMetadata` out, with no knowledge of the queue,
mpv, or playback state. `player.py` owns deciding *when* to call this
and mutating the queue with the result — keeps this module trivially
unit-testable and safe to reuse (e.g. a future "start radio from this
track" feature) without dragging playback state along.
"""

from __future__ import annotations

import random
from collections import Counter

from metadata import TrackMetadata

# How many tracks to pull in per top-up. Small on purpose — top-ups
# fire repeatedly as playback continues, so there's no need (and no
# benefit) to grab a lot at once.
DEFAULT_BATCH_SIZE = 5

# How many of the most-recently-played tracks influence artist/genre
# weighting for the next batch. Bounded so a long session doesn't let
# one very old artist keep dominating the mix forever.
HISTORY_WINDOW = 20

# Weight bonuses for matching the recent-history taste signal. Additive,
# not multiplicative, so a track with no match at all still has a
# nonzero (base) chance of being picked — the mix should stay varied,
# not collapse to only the exact same artist on repeat.
ARTIST_MATCH_WEIGHT = 3.0
GENRE_MATCH_WEIGHT = 1.5
BASE_WEIGHT = 1.0


class RadioMixer:
    """Picks the next batch of "radio" tracks from a library."""

    def __init__(self, batch_size: int = DEFAULT_BATCH_SIZE) -> None:
        self.batch_size = batch_size

    def next_batch(
        self,
        library: list[TrackMetadata],
        queue_paths: set[str],
        history: list[TrackMetadata],
    ) -> list[TrackMetadata]:
        """Return up to `batch_size` tracks from `library` that aren't
        already in `queue_paths`, weighted toward artists/genres seen
        in the tail of `history` (oldest-first list of played tracks —
        only the most recent `HISTORY_WINDOW` entries matter).

        Never raises on a small/empty library or history — worst case
        it returns fewer tracks than `batch_size`, down to an empty
        list, which the caller (`player.py`) already treats as "no
        more material available right now" rather than an error.
        """
        # Online-resolved tracks carry a short-lived signed stream URL
        # as `path` (see metadata.py's `is_online` docstring) — never
        # safe to silently re-add to a *local* radio mix days later
        # from a stale library snapshot, so they're excluded here.
        pool = [
            t for t in library
            if not t.is_online and t.path not in queue_paths
        ]
        if not pool:
            return []

        recent = history[-HISTORY_WINDOW:]
        artist_counts = Counter(t.artist for t in recent if t.artist)
        genre_counts = Counter(t.genre for t in recent if t.genre)

        def weight(track: TrackMetadata) -> float:
            w = BASE_WEIGHT
            w += ARTIST_MATCH_WEIGHT * artist_counts.get(track.artist, 0)
            w += GENRE_MATCH_WEIGHT * genre_counts.get(track.genre, 0)
            return w

        candidates = list(pool)
        weights = [weight(t) for t in candidates]
        chosen: list[TrackMetadata] = []

        n = min(self.batch_size, len(candidates))
        for _ in range(n):
            total = sum(weights)
            if total <= 0:
                # Every remaining weight collapsed to 0 — can't happen
                # given BASE_WEIGHT's floor, but a plain random pick is
                # a safe fallback rather than raising on `random.uniform(0, 0)`.
                idx = random.randrange(len(candidates))
            else:
                pick = random.uniform(0, total)
                running = 0.0
                idx = len(candidates) - 1  # float rounding safety net
                for i, w in enumerate(weights):
                    running += w
                    if running >= pick:
                        idx = i
                        break
            chosen.append(candidates.pop(idx))
            weights.pop(idx)

        return chosen
