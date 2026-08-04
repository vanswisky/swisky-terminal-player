"""
mouse_handler.py
=================
Enables terminal SGR mouse reporting and decodes the raw escape
sequences forwarded by `keyboard_handler.py` (prefixed `"MOUSE:"`)
into structured `MouseEvent`s: clicks on the progress bar seek,
clicks on volume change volume, clicks on transport buttons execute
actions, and scroll wheel events over lyrics/playlist scroll them.

`ui.py` is responsible for hit-testing — this module only decodes
*what happened*, not *what it means in the current layout*.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from enum import Enum, auto

_ENABLE_MOUSE = "\x1b[?1000h\x1b[?1006h"
_DISABLE_MOUSE = "\x1b[?1000l\x1b[?1006l"

_SGR_RE = re.compile(r"\[<(\d+);(\d+);(\d+)([Mm])")


class MouseAction(Enum):
    PRESS = auto()
    RELEASE = auto()
    SCROLL_UP = auto()
    SCROLL_DOWN = auto()


@dataclass(slots=True)
class MouseEvent:
    action: MouseAction
    column: int  # 1-indexed terminal column
    row: int     # 1-indexed terminal row
    button: int


def enable_mouse_reporting() -> None:
    sys.stdout.write(_ENABLE_MOUSE)
    sys.stdout.flush()


def disable_mouse_reporting() -> None:
    sys.stdout.write(_DISABLE_MOUSE)
    sys.stdout.flush()


def parse_mouse_sequence(tagged: str) -> MouseEvent | None:
    """Decode a `"MOUSE:[<...M"` token from keyboard_handler into a MouseEvent."""
    if not tagged.startswith("MOUSE:"):
        return None
    raw = tagged[len("MOUSE:"):]
    match = _SGR_RE.match(raw)
    if not match:
        return None

    code, col, row, terminator = match.groups()
    code = int(code)
    col, row = int(col), int(row)

    if code == 64:
        return MouseEvent(MouseAction.SCROLL_UP, col, row, code)
    if code == 65:
        return MouseEvent(MouseAction.SCROLL_DOWN, col, row, code)

    action = MouseAction.PRESS if terminator == "M" else MouseAction.RELEASE
    return MouseEvent(action, col, row, code)
