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
# replace the current file with a new load(). Only a "natural EOF"
# reason means "this track played through to the end and playback
# should advance". In real python-mpv, `event_callback("end-file")`
# hands back the raw ctypes `MpvEvent` struct (NOT a dict!) — the
# reason lives at `event.data.reason` as a plain C int matching
# libmpv's `mpv_end_file_reason` enum, where EOF == 0. An earlier
# version of this file assumed a dict shape (`event["event"]["reason"]`
# holding the string "eof"), which never matched the real struct at
# all — `isinstance(event, dict)` was always False, so this flag never
# got set and auto-advance silently never fired. See
# `_extract_end_file_reason` below for the actual, verified shape.
_EOF_REASON_NATURAL_END = 0


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
            if not self._is_natural_eof(reason):
                # Caused by our own stop()/load() calls, or a real
                # error — never something we should auto-advance for.
                return
            with self._lock:
                self._eof_pending = True

    @staticmethod
    def _extract_end_file_reason(event):  # noqa: ANN001
        """Real `python-mpv` hands `event_callback` the raw ctypes
        `MpvEvent` struct, not a dict. `event.data` casts it to the
        event-specific payload — `MpvEventEndFile` for this event type
        — whose `.reason` field is a plain C int (0 == natural EOF,
        matching libmpv's `mpv_end_file_reason` / python-mpv's
        `MpvEventEndFile.EOF`). Verified directly against python-mpv's
        source (mpv.py: `MpvEventEndFile`, `event_callback`).

        Also accepts a dict shape as a fallback, in case some wrapper
        or test double serializes events differently — better to
        handle both than to silently match neither, which is exactly
        the bug this replaced (the previous version required a dict
        and never got one, so this flag never fired).
        """
        data = getattr(event, "data", None)
        if data is not None and hasattr(data, "reason"):
            return data.reason
        if isinstance(event, dict):
            if "reason" in event:
                return event["reason"]
            inner = event.get("event")
            if isinstance(inner, dict):
                return inner.get("reason")
        return None

    @staticmethod
    def _is_natural_eof(reason) -> bool:
        """Normalizes whatever shape `reason` came in as (raw int 0,
        the string "eof", or an enum member) to a single yes/no answer,
        so this doesn't silently break again if python-mpv changes how
        it represents this between versions.
        """
        if reason is None:
            return False
        if isinstance(reason, bool):
            return False
        if isinstance(reason, int):
            return reason == _EOF_REASON_NATURAL_END
        if isinstance(reason, str):
            return reason.lower() == "eof"
        name = getattr(reason, "name", None)
        if isinstance(name, str) and name.lower() == "eof":
            return True
        value = getattr(reason, "value", None)
        if isinstance(value, str) and value.lower() == "eof":
            return True
        if isinstance(value, int) and value == _EOF_REASON_NATURAL_END:
            return True
        return False

    # -- transport ---------------------------------------------------

    def load(self, path: str, http_headers: list[str] | None = None) -> None:
        """`http_headers` is a list of pre-formatted `"Key: Value"`
        entries — set for online-resolved tracks whose CDN 403s
        without a matching User-Agent/Referer, `None`/empty for local
        files.

        This is set as a genuine mpv list-option *property*
        (`self._mpv["http-header-fields"] = [...]`), not passed as a
        per-file option through `loadfile(..., http_header_fields=...)`.
        The latter builds one big `"key=value,key=value"` options
        string for the whole `loadfile` call, and mpv's own
        `http-header-fields` value is *itself* a comma-separated list
        of headers — so a header value containing a comma collides
        with the options-string's own comma separator, and mpv rejects
        the entire command with "Invalid value for mpv parameter"
        (error -4). Setting it as a property instead hands mpv a
        proper list (python-mpv converts a Python list straight into
        an MPV node array), so there's no text to escape in the first
        place.
        """
        with self._lock:
            self._state.path = path
            self._eof_pending = False
        # Always set (even to `[]`) so headers from a previous online
        # track can't leak into the next (possibly local) one.
        self._mpv["http-header-fields"] = list(http_headers) if http_headers else []
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
            # Without this, `stop()` (queue genuinely ran out, no
            # repeat/radio to fall back on) left `paused` at whatever
            # it was mid-playback — usually False. The UI reads
            # `state.paused` to decide "still playing" vs "stopped"
            # (transport icon, spectrum decay-vs-analyze in ui.py), so
            # it kept rendering as if a track were actively playing
            # against a dead mpv instance: no error, no advance, just
            # a frozen-looking screen. Marking paused here makes the
            # UI reflect reality the same frame playback actually ends.
            self._state.paused = True

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
