"""
queue_manager.py
=================
The *actual* playback queue — distinct from the playlist/library
browser. Tracks "play next" requests, manual reordering, and the
current playback cursor. `player.py` calls `advance()` to get the
next track according to repeat/shuffle rules.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from config import RepeatMode
from metadata import TrackMetadata


@dataclass(slots=True)
class QueueManager:
    items: list[TrackMetadata] = field(default_factory=list)
    cursor: int = -1  # index of currently playing item, -1 = nothing loaded
    _shuffle_order: list[int] = field(default_factory=list)

    # -- building the queue -----------------------------------------------

    def load(self, tracks: list[TrackMetadata], start_index: int = 0) -> None:
        self.items = list(tracks)
        self.cursor = start_index if self.items else -1
        self._rebuild_shuffle_order()

    def add_next(self, track: TrackMetadata) -> None:
        insert_at = self.cursor + 1 if self.cursor >= 0 else len(self.items)
        self.items.insert(insert_at, track)
        self._rebuild_shuffle_order()

    def add_end(self, track: TrackMetadata) -> None:
        self.items.append(track)
        self._rebuild_shuffle_order()

    def remove(self, index: int) -> None:
        if 0 <= index < len(self.items):
            del self.items[index]
            if index < self.cursor:
                self.cursor -= 1
            elif index == self.cursor:
                self.cursor = min(self.cursor, len(self.items) - 1)
            self._rebuild_shuffle_order()

    def move_up(self, index: int) -> None:
        if index <= 0 or index >= len(self.items):
            return
        self.items[index - 1], self.items[index] = self.items[index], self.items[index - 1]
        if self.cursor == index:
            self.cursor -= 1
        elif self.cursor == index - 1:
            self.cursor += 1

    def move_down(self, index: int) -> None:
        self.move_up(index + 1)

    def clear(self) -> None:
        self.items.clear()
        self.cursor = -1
        self._shuffle_order.clear()

    def jump_to(self, index: int) -> TrackMetadata | None:
        if 0 <= index < len(self.items):
            self.cursor = index
            return self.items[index]
        return None

    # -- current / navigation ---------------------------------------------

    def current(self) -> TrackMetadata | None:
        if 0 <= self.cursor < len(self.items):
            return self.items[self.cursor]
        return None

    def advance(self, repeat: RepeatMode, shuffle: bool) -> TrackMetadata | None:
        """Compute and move to the next track per repeat/shuffle rules."""
        if not self.items:
            return None

        if repeat == RepeatMode.ONE:
            return self.current()

        if shuffle:
            return self._advance_shuffle(repeat)

        if self.cursor + 1 < len(self.items):
            self.cursor += 1
            return self.current()

        if repeat == RepeatMode.ALL:
            self.cursor = 0
            return self.current()

        return None  # end of queue

    def peek_next(self, repeat: RepeatMode, shuffle: bool) -> TrackMetadata | None:
        """Read-only lookahead: whatever `advance()` would move to
        right now, under the same repeat/shuffle rules, *without*
        touching `cursor` or the shuffle order. Used by callers that
        want to warm something up ahead of time for whatever's coming
        up next — `stream_cache.StreamPrefetcher`'s next-track
        download, and `ui.py`'s Autoplay pre-fetch (which needs to
        know when the *current* track is the last one queued) — while
        leaving actual playback navigation untouched.
        """
        if not self.items:
            return None
        if repeat == RepeatMode.ONE:
            return self.current()
        if shuffle:
            if not self._shuffle_order:
                return None
            try:
                pos = self._shuffle_order.index(self.cursor)
            except ValueError:
                pos = -1
            if pos + 1 < len(self._shuffle_order):
                return self.items[self._shuffle_order[pos + 1]]
            if repeat == RepeatMode.ALL:
                return self.items[self._shuffle_order[0]]
            return None
        if self.cursor + 1 < len(self.items):
            return self.items[self.cursor + 1]
        if repeat == RepeatMode.ALL:
            return self.items[0]
        return None

    def previous(self, shuffle: bool) -> TrackMetadata | None:
        if not self.items:
            return None
        if shuffle:
            pos = self._shuffle_order.index(self.cursor) if self.cursor in self._shuffle_order else 0
            pos = max(0, pos - 1)
            self.cursor = self._shuffle_order[pos]
        else:
            self.cursor = max(0, self.cursor - 1)
        return self.current()

    # -- shuffle bookkeeping -----------------------------------------------

    def _rebuild_shuffle_order(self) -> None:
        order = list(range(len(self.items)))
        random.shuffle(order)
        self._shuffle_order = order

    def _advance_shuffle(self, repeat: RepeatMode) -> TrackMetadata | None:
        if not self._shuffle_order:
            self._rebuild_shuffle_order()
        try:
            pos = self._shuffle_order.index(self.cursor)
        except ValueError:
            pos = -1
        if pos + 1 < len(self._shuffle_order):
            self.cursor = self._shuffle_order[pos + 1]
            return self.current()
        if repeat == RepeatMode.ALL:
            self._rebuild_shuffle_order()
            self.cursor = self._shuffle_order[0]
            return self.current()
        return None
