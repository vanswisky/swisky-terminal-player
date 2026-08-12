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
        self._reshuffle_keeping_current()

    def add_next(self, track: TrackMetadata) -> None:
        insert_at = self.cursor + 1 if self.cursor >= 0 else len(self.items)
        self.items.insert(insert_at, track)
        self._reshuffle_keeping_current()

    def add_end(self, track: TrackMetadata) -> None:
        self.items.append(track)
        new_index = len(self.items) - 1
        if self._shuffle_order:
            # Fold the new track into the *upcoming* tail of the
            # current shuffle round instead of a full reshuffle. A
            # full reshuffle would scramble tracks already played back
            # into "upcoming" too, so radio.py's top-ups (which call
            # this repeatedly during active playback) would keep
            # re-serving songs from earlier this session. Inserting
            # only after the current position keeps play history
            # intact and the new track genuinely new.
            try:
                pos = self._shuffle_order.index(self.cursor)
            except ValueError:
                pos = -1
            insert_at = random.randint(pos + 1, len(self._shuffle_order))
            self._shuffle_order.insert(insert_at, new_index)
        else:
            self._reshuffle_keeping_current()

    def remove(self, index: int) -> None:
        if 0 <= index < len(self.items):
            del self.items[index]
            if index < self.cursor:
                self.cursor -= 1
            elif index == self.cursor:
                self.cursor = min(self.cursor, len(self.items) - 1)
            self._reshuffle_keeping_current()

    def move_up(self, index: int) -> None:
        if index <= 0 or index >= len(self.items):
            return
        self.items[index - 1], self.items[index] = self.items[index], self.items[index - 1]
        if self.cursor == index:
            self.cursor -= 1
        elif self.cursor == index - 1:
            self.cursor += 1
        # The swap silently invalidated any `_shuffle_order` entries
        # pointing at these two positions (same index, different track
        # now). Cheap to just re-pin/reshuffle from here rather than
        # remap two values in place — manual reordering is infrequent
        # enough that resetting the round is the safer trade.
        self._reshuffle_keeping_current()

    def move_down(self, index: int) -> None:
        self.move_up(index + 1)

    def clear(self) -> None:
        self.items.clear()
        self.cursor = -1
        self._shuffle_order.clear()

    def jump_to(self, index: int) -> TrackMetadata | None:
        if 0 <= index < len(self.items):
            self.cursor = index
            # A direct jump (Queue screen Enter, "play next" + jump,
            # etc.) isn't a step through the existing shuffle order —
            # treat it like the start of a fresh round from here so a
            # later `advance()` doesn't misjudge which tracks already
            # played, the same class of bug fixed for load/add/remove
            # below.
            self._reshuffle_keeping_current()
            return self.items[index]
        return None

    def paths(self) -> set[str]:
        """Paths of every track currently in the queue — used by
        radio.py to avoid re-adding a track that's already queued.
        """
        return {t.path for t in self.items}

    def remaining_ahead(self, shuffle: bool) -> int:
        """How many not-yet-played tracks are still queued after the
        current one, honoring the active shuffle setting. Used by
        radio.py to decide when the queue is running low enough to
        top up with fresh tracks — checked *before* `advance()`, so it
        must not mutate any state.
        """
        if not self.items:
            return 0
        if shuffle:
            if not self._shuffle_order:
                return max(0, len(self.items) - 1)
            try:
                pos = self._shuffle_order.index(self.cursor)
            except ValueError:
                pos = -1
            return max(0, len(self._shuffle_order) - pos - 1)
        return max(0, len(self.items) - 1 - self.cursor)

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

    def _rebuild_shuffle_order(self, avoid_first: int | None = None) -> None:
        """Build a brand-new random permutation of every item index —
        used only when a shuffle round has genuinely finished and a
        fresh one is starting (see `_advance_shuffle`). Deliberately
        does NOT pin `self.cursor` to position 0: the point here is
        the *previous* track can legitimately come up again anywhere
        except immediately (handled by `avoid_first`), unlike
        `_reshuffle_keeping_current`, which is for "the item list
        itself changed, keep going from where we are."
        """
        order = list(range(len(self.items)))
        random.shuffle(order)
        # Avoid replaying the same track back-to-back when a shuffle
        # cycle loops into a fresh one (e.g. last track of round N
        # landing first in round N+1).
        if avoid_first is not None and len(order) > 1 and order[0] == avoid_first:
            swap_with = random.randint(1, len(order) - 1)
            order[0], order[swap_with] = order[swap_with], order[0]
        self._shuffle_order = order

    def _reshuffle_keeping_current(self) -> None:
        """Rebuild the shuffle order so the *currently loaded* track
        (`self.cursor`) sits at position 0 and every other item is
        shuffled after it. Used whenever the item list itself changes
        (load/add/remove/move/jump) rather than when a round finishes
        naturally.

        Pinning the current track matters: a plain full reshuffle can
        land the current track anywhere in the new permutation, which
        made `_advance_shuffle` treat every item before that random
        slot as "already played this round" even though nothing had
        actually played there yet — cutting rounds short (sometimes to
        just 1-2 tracks) and, worse, making `add_end`'s "don't touch
        already-played history" guarantee meaningless, since that
        history boundary was wrong to begin with.
        """
        n = len(self.items)
        if n == 0:
            self._shuffle_order = []
            return
        if not (0 <= self.cursor < n):
            order = list(range(n))
            random.shuffle(order)
            self._shuffle_order = order
            return
        rest = [i for i in range(n) if i != self.cursor]
        random.shuffle(rest)
        self._shuffle_order = [self.cursor] + rest

    def _advance_shuffle(self, repeat: RepeatMode) -> TrackMetadata | None:
        if not self._shuffle_order:
            self._reshuffle_keeping_current()
        try:
            pos = self._shuffle_order.index(self.cursor)
        except ValueError:
            pos = -1
        if pos + 1 < len(self._shuffle_order):
            self.cursor = self._shuffle_order[pos + 1]
            return self.current()
        # Shuffle exhausted the list. Spotify-style: shuffle keeps
        # looping on its own even with repeat OFF — repeat ALL/OFF
        # both reshuffle here, repeat ONE never reaches this branch
        # (handled earlier in advance()). Only a genuinely empty
        # queue (len<=1) has nowhere new to go.
        if len(self.items) > 1:
            last = self.cursor
            self._rebuild_shuffle_order(avoid_first=last)
            self.cursor = self._shuffle_order[0]
            return self.current()
        if repeat == RepeatMode.ALL:
            self._rebuild_shuffle_order()
            self.cursor = self._shuffle_order[0]
            return self.current()
        return None
