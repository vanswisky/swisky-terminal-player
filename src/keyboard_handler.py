"""
keyboard_handler.py
====================
Non-blocking raw-terminal keyboard reader. Runs on its own daemon
thread, puts parsed logical key names (see `constants.KEY_*`) onto a
`queue.Queue` that the UI thread drains once per frame.

Handles multi-byte ANSI escape sequences for arrow keys and their
Ctrl-modified variants (`\x1b[1;5C` etc.), which is why this can't
just be a naive single-byte read loop.

Reads from `/dev/tty` directly rather than `sys.stdin`. This matters:
`python-mpv`/libmpv can enable its own terminal-input feature and read
raw bytes from fd 0 (stdin) even when told not to via constructor
options, if that option isn't honored for whatever reason on a given
mpv build. Two readers racing over the same fd means bytes can go
missing or arrive split, which is exactly what turns "press arrow key"
into "ESC fires, then two stray leftover characters show up". Opening
/dev/tty gives this handler its own independent file descriptor to the
controlling terminal, so it no longer matters what mpv does with fd 0
at all (see `main.py`, which also redirects fd 0 to /dev/null as a
second line of defense).
"""

from __future__ import annotations

import logging
import os
import queue
import select
import sys
import termios
import threading
import tty
from typing import Optional

logger = logging.getLogger(__name__)

# Maps raw escape sequences (after the initial ESC) to logical key names.
_ESCAPE_SEQUENCES = {
    "[A": "UP",
    "[B": "DOWN",
    "[C": "RIGHT",
    "[D": "LEFT",
    "[1;5C": "CTRL_RIGHT",
    "[1;5D": "CTRL_LEFT",
    "[1;3C": "ALT_RIGHT",
    "[1;3D": "ALT_LEFT",
}

# Following bytes of a CSI sequence (or an SGR mouse report) arrive from
# the terminal as one burst, but this thread still has to get scheduled
# to read them. Too tight a timeout here reads as "nothing else is
# coming" even though it is, which turns every arrow-key press and every
# mouse click into a bare ESC (opening Settings) plus stray leftover
# characters. 50ms is still well under human perception for a genuine
# standalone ESC press.
_SEQUENCE_TIMEOUT = 0.05


class KeyboardHandler:
    def __init__(self) -> None:
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tty_fd: Optional[int] = None
        self._owns_fd = False
        self._original_settings = None

    def start(self) -> None:
        # Prefer a dedicated /dev/tty fd (see module docstring) — falls
        # back to sys.stdin only if /dev/tty genuinely isn't available
        # (e.g. running with no controlling terminal at all).
        try:
            self._tty_fd = os.open("/dev/tty", os.O_RDONLY | os.O_NONBLOCK)
            self._owns_fd = True
        except OSError as exc:
            logger.warning("Could not open /dev/tty (%s); falling back to stdin", exc)
            self._tty_fd = sys.stdin.fileno()
            self._owns_fd = False

        try:
            self._original_settings = termios.tcgetattr(self._tty_fd)
            tty.setcbreak(self._tty_fd)
        except termios.error as exc:
            logger.warning("Could not set raw terminal mode: %s", exc)

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._original_settings is not None:
            try:
                termios.tcsetattr(self._tty_fd, termios.TCSADRAIN, self._original_settings)
            except termios.error:
                pass
        if self._owns_fd and self._tty_fd is not None:
            try:
                os.close(self._tty_fd)
            except OSError:
                pass

    def poll(self) -> Optional[str]:
        """Non-blocking: returns the next logical key name, or None."""
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def drain(self) -> list[str]:
        keys = []
        while True:
            key = self.poll()
            if key is None:
                break
            keys.append(key)
        return keys

    # -- internal ---------------------------------------------------------

    def _read_one(self) -> Optional[str]:
        """Read exactly one byte from the tty fd, or None if nothing is
        available right now. O_NONBLOCK means a "no data" read raises
        BlockingIOError instead of blocking — callers only get here
        after select() already confirmed readability, so this should
        normally succeed immediately.
        """
        try:
            data = os.read(self._tty_fd, 1)
        except BlockingIOError:
            return None
        except OSError:
            return None
        if not data:
            return None
        return data.decode("utf-8", errors="replace")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            ready, _, _ = select.select([self._tty_fd], [], [], 0.05)
            if not ready:
                continue
            char = self._read_one()
            if char is None:
                continue
            if char == "\x1b":
                self._queue.put(self._read_escape_sequence())
            elif char == "\x10":  # Ctrl+P
                self._queue.put("CTRL_P")
            elif char == " ":
                self._queue.put("SPACE")
            elif char == "\t":
                self._queue.put("TAB")
            elif char in ("\r", "\n"):
                self._queue.put("ENTER")
            elif char == "\x7f" or char == "\x08":
                self._queue.put("BACKSPACE")
            elif char == "\x03":  # Ctrl+C
                self._queue.put("QUIT")
            else:
                self._queue.put(char)

    def _read_escape_sequence(self) -> str:
        # Might be a lone ESC keypress (menu), or the start of a longer
        # CSI sequence for arrows / modified arrows / SGR mouse reports.
        ready, _, _ = select.select([self._tty_fd], [], [], _SEQUENCE_TIMEOUT)
        if not ready:
            return "ESC"
        seq = self._read_one()
        if seq != "[":
            return "ESC"
        seq = "["
        while True:
            ready, _, _ = select.select([self._tty_fd], [], [], _SEQUENCE_TIMEOUT)
            if not ready:
                break
            ch = self._read_one()
            if ch is None:
                break
            seq += ch
            if ch.isalpha() or ch == "~":
                break
        # SGR mouse reports look like "[<btn;col;rowM" / "...m" — forward
        # the raw sequence, tagged, for mouse_handler.py to decode.
        if seq.startswith("[<"):
            return f"MOUSE:{seq}"
        return _ESCAPE_SEQUENCES.get(seq, "ESC")
