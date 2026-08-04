"""
audio_engine.py
================
Thin, thread-safe wrapper around `python-mpv` (libmpv). This is the
*only* module that talks to mpv directly — everything else (player.py,
visualizer.py, ui.py) goes through this class, so the playback backend
could be swapped (e.g. for pygame) without touching the rest of the app.

Runs mpv in `--vid=no --idle` audio-only mode. Property-change
callbacks fire on mpv's own event thread; we just store state under a
lock. The one exception is end-of-file: see `consume_eof()` below for
why that is deliberately *polled* by the caller instead of pushed via
a callback.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from dataclasses import dataclass
from typing import Optional

import mpv

logger = logging.getLogger(__name__)

# mpv's "end-file" event fires for several reasons, not just a track
# finishing naturally — it also fires when we call stop() ourselves or
# replace the current file with a new load(). Only "eof" means "this
# track played through to the end and playback should advance".
_EOF_REASON_NATURAL_END = "eof"


@dataclass(slots=True)
class EngineState:
    path: Optional[str] = None
    duration: float = 0.0
    position: float = 0.0
    paused: bool = True
    volume: int = 80
    speed: float = 1.0


class AudioEngine:
    """Wraps a single mpv instance for gapless-ish single-track playback."""

    def __init__(self, initial_volume: int = 80) -> None:
        self._lock = threading.RLock()
        self._state = EngineState(volume=initial_volume)
        # Set by the end-file event (mpv's own thread) when a track
        # finishes naturally, cleared by `consume_eof()` (the caller's
        # thread). This hand-off is the whole point: mpv's event thread
        # only ever flips a flag under a lock — it never issues another
        # mpv command itself. Calling a blocking command like `play()`
        # from *inside* mpv's own event callback can deadlock libmpv's
        # single-threaded event loop (the callback would be waiting on
        # a reply that only that same thread can deliver). Every actual
        # "go to the next track" call therefore happens on the app's
        # main thread instead, via `Player.poll()`.
        self._eof_pending = False

        self._mpv = mpv.MPV(
            vid=False,
            ytdl=False,
            idle=True,
            input_default_bindings=False,
            input_vo_keyboard=False,
            osc=False,
            terminal=False,       # mpv otherwise grabs stdin itself for its own
            input_terminal=False,  # keybindings, racing keyboard_handler.py for
            config=False,           # the same bytes — this is what silently ate
        )                            # some of the app's keypresses.
        self._mpv.volume = initial_volume

        @self._mpv.property_observer("time-pos")
        def _on_time_pos(_name, value):  # noqa: ANN001
            with self._lock:
                self._state.position = value or 0.0

        @self._mpv.property_observer("duration")
        def _on_duration(_name, value):  # noqa: ANN001
            with self._lock:
                self._state.duration = value or 0.0

        @self._mpv.property_observer("pause")
        def _on_pause(_name, value):  # noqa: ANN001
            with self._lock:
                self._state.paused = bool(value)

        @self._mpv.event_callback("end-file")
        def _on_end_file(event):  # noqa: ANN001
            reason = self._extract_end_file_reason(event)
            if reason != _EOF_REASON_NATURAL_END:
                # Caused by our own stop()/load() calls, or a real
                # error — never something we should auto-advance for.
                return
            with self._lock:
                self._eof_pending = True

    @staticmethod
    def _extract_end_file_reason(event) -> Optional[str]:  # noqa: ANN001
        """python-mpv has represented this a couple of different ways
        across versions — be liberal about what we accept rather than
        silently never matching (which is what let *every* end-file,
        regardless of cause, fall through to "treat as natural EOF").
        """
        if not isinstance(event, dict):
            return None
        if "reason" in event:
            return event["reason"]
        inner = event.get("event")
        if isinstance(inner, dict):
            return inner.get("reason")
        return None

    # -- transport ---------------------------------------------------

    def load(self, path: str) -> None:
        with self._lock:
            self._state.path = path
            self._eof_pending = False
        self._mpv.play(path)
        self._mpv.pause = False

    def play(self) -> None:
        self._mpv.pause = False

    def pause(self) -> None:
        self._mpv.pause = True

    def toggle_pause(self) -> None:
        self._mpv.pause = not self._mpv.pause

    def stop(self) -> None:
        self._mpv.stop()
        with self._lock:
            self._state.position = 0.0

    def seek(self, seconds: float, relative: bool = True) -> None:
        try:
            self._mpv.seek(seconds, reference="relative" if relative else "absolute")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Seek failed: %s", exc)

    def seek_to(self, seconds: float) -> None:
        self.seek(seconds, relative=False)

    # -- volume / speed ------------------------------------------------

    def set_volume(self, volume: int) -> None:
        volume = max(0, min(150, volume))
        self._mpv.volume = volume
        with self._lock:
            self._state.volume = volume

    def set_speed(self, speed: float) -> None:
        self._mpv.speed = speed
        with self._lock:
            self._state.speed = speed

    def set_mute(self, muted: bool) -> None:
        self._mpv.mute = muted

    # -- state -----------------------------------------------------------

    def snapshot(self) -> EngineState:
        with self._lock:
            return dataclasses.replace(self._state)

    def consume_eof(self) -> bool:
        """Returns True exactly once per track that has naturally
        finished playing, then clears the flag. Must be polled from
        the same thread that's allowed to issue mpv commands (i.e. the
        app's main thread) — see the comment on `_eof_pending` above.
        """
        with self._lock:
            if self._eof_pending:
                self._eof_pending = False
                return True
            return False

    def shutdown(self) -> None:
        try:
            self._mpv.terminate()
        except Exception:  # noqa: BLE001
            pass
