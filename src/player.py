"""
player.py
=========
High-level playback controller. Coordinates `AudioEngine` (mpv),
`QueueManager` (what plays next), and `LyricsManager` (sync), and
exposes the single API the UI/keyboard/command-palette layers call:
play, pause, next, previous, seek, volume, repeat, shuffle.

Also fires a `on_track_changed` callback so `ui.py` knows to trigger
an ASCII re-render and lyrics reload without polling.

`poll()` must be called once per frame from the app's main thread —
that's how a naturally-finished track turns into "advance to the next
one". See the comment on `AudioEngine._eof_pending` for why this is
polled rather than pushed straight from mpv's own event thread.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from audio_engine import AudioEngine, EngineState
from config import PlaybackConfig, RepeatMode
from lyrics_manager import LyricsManager
from metadata import TrackMetadata
from queue_manager import QueueManager

logger = logging.getLogger(__name__)

TrackChangedCallback = Callable[[Optional[TrackMetadata]], None]


class Player:
    def __init__(self, config: PlaybackConfig, lyrics_manager: LyricsManager) -> None:
        self.config = config
        self.engine = AudioEngine(initial_volume=config.volume)
        self.queue = QueueManager()
        self.lyrics = lyrics_manager

        self._lock = threading.RLock()
        self._on_track_changed: list[TrackChangedCallback] = []
        self.muted = False
        self._pre_mute_volume = config.volume

    # -- events -----------------------------------------------------------

    def on_track_changed(self, callback: TrackChangedCallback) -> None:
        self._on_track_changed.append(callback)

    def _notify_track_changed(self) -> None:
        track = self.queue.current()
        for cb in list(self._on_track_changed):
            try:
                cb(track)
            except Exception:  # noqa: BLE001
                logger.exception("track_changed callback raised")

    # -- queue / loading ----------------------------------------------------

    def load_queue(self, tracks: list[TrackMetadata], start_index: int = 0) -> None:
        with self._lock:
            self.queue.load(tracks, start_index)
            self._play_current()

    def play_index(self, index: int) -> None:
        """Jump straight to a specific position in the queue (e.g. the
        user pressing Enter on a row in the Queue screen). Public
        counterpart to the old direct `queue.jump_to()` + private
        `_play_current()` call `ui.py` used to make.
        """
        with self._lock:
            if self.queue.jump_to(index) is not None:
                self._play_current()

    def play_online_next(self, track: TrackMetadata) -> None:
        """Insert an online-resolved track right after whatever's
        currently playing and jump to it immediately — used by the
        Online Search screen's Enter key. Doesn't disturb the rest of
        the queue.
        """
        with self._lock:
            self.queue.add_next(track)
            self.play_index(self.queue.cursor + 1)

    def _load_track(self, track: TrackMetadata) -> None:
        """Shared by `_play_current`/`next`/`previous` so online tracks'
        `stream_headers` (needed by some CDNs to avoid a 403) get
        forwarded to mpv the same way regardless of which of those
        three called us.
        """
        self.engine.load(track.path, http_headers=track.stream_headers)
        self.engine.set_speed(self.config.speed)
        self.lyrics.load_for_track(track)
        self._notify_track_changed()

    def _play_current(self) -> None:
        track = self.queue.current()
        if track is None:
            self.engine.stop()
            self._notify_track_changed()
            return
        self._load_track(track)

    # -- transport ------------------------------------------------------

    def toggle_play_pause(self) -> None:
        self.engine.toggle_pause()

    def pause(self) -> None:
        self.engine.pause()

    def resume(self) -> None:
        self.engine.play()

    def stop(self) -> None:
        self.engine.stop()

    def next(self) -> None:
        with self._lock:
            track = self.queue.advance(self.config.repeat, self.config.shuffle)
            if track is None:
                self.engine.stop()
                self._notify_track_changed()
                return
            self._load_track(track)

    def previous(self) -> None:
        with self._lock:
            # If more than 3s into the track, restart it instead of
            # jumping back a track (standard music-player UX).
            if self.engine.snapshot().position > 3.0:
                self.engine.seek_to(0)
                return
            track = self.queue.previous(self.config.shuffle)
            if track is None:
                return
            self._load_track(track)

    def poll(self) -> None:
        """Call once per frame from the main thread. Detects a track
        that finished playing naturally and advances the queue.

        Deliberately not driven by mpv's own end-file callback — that
        callback runs on mpv's internal event thread, and issuing
        another blocking mpv command (loading the next track) from
        inside it can deadlock libmpv's single-threaded event loop.
        Polling keeps every mpv command on one, safe, predictable
        thread. This is also what used to make the player "only play
        one song": the auto-advance call from that thread would hang
        rather than actually loading the next track.
        """
        if self.engine.consume_eof():
            self.next()

    # -- seeking -----------------------------------------------------

    def seek_relative(self, seconds: float) -> None:
        self.engine.seek(seconds, relative=True)

    def seek_absolute(self, seconds: float) -> None:
        self.engine.seek_to(seconds)

    def seek_to_fraction(self, fraction: float) -> None:
        """Used for mouse-click seeking on the progress bar (fraction 0..1)."""
        state = self.engine.snapshot()
        if state.duration > 0:
            self.engine.seek_to(max(0.0, min(1.0, fraction)) * state.duration)

    # -- volume ----------------------------------------------------------

    def volume_up(self, step: int | None = None) -> None:
        step = step or self.config.volume_step
        self.set_volume(self.engine.snapshot().volume + step)

    def volume_down(self, step: int | None = None) -> None:
        step = step or self.config.volume_step
        self.set_volume(self.engine.snapshot().volume - step)

    def set_volume(self, volume: int) -> None:
        volume = max(0, min(150, volume))
        self.engine.set_volume(volume)
        self.config.volume = volume

    def toggle_mute(self) -> None:
        self.muted = not self.muted
        self.engine.set_mute(self.muted)

    # -- modes -----------------------------------------------------------

    def cycle_repeat(self) -> RepeatMode:
        order = [RepeatMode.OFF, RepeatMode.ALL, RepeatMode.ONE]
        idx = (order.index(self.config.repeat) + 1) % len(order)
        self.config.repeat = order[idx]
        return self.config.repeat

    def toggle_shuffle(self) -> bool:
        self.config.shuffle = not self.config.shuffle
        return self.config.shuffle

    def set_speed(self, speed: float) -> None:
        self.config.speed = speed
        self.engine.set_speed(speed)

    # -- state -------------------------------------------------------

    def snapshot(self) -> EngineState:
        return self.engine.snapshot()

    def current_track(self) -> TrackMetadata | None:
        return self.queue.current()

    def shutdown(self) -> None:
        self.engine.shutdown()
