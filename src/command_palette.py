"""
command_palette.py
===================
A VS Code-style fuzzy command palette. Commands are plain strings
(`"play"`, `"seek 01:35"`, `"volume 80"`, `"theme purple"`, ...)
parsed and dispatched here; `ui.py` only needs to open the palette
and forward the submitted text to `CommandPalette.execute`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

Handler = Callable[[re.Match], None]


@dataclass(slots=True)
class Command:
    pattern: re.Pattern
    description: str
    handler: Handler


class CommandPalette:
    """Owns the command registry and fuzzy-matches palette input against it."""

    STATIC_SUGGESTIONS = [
        "play", "pause", "next", "previous", "seek 01:35", "volume 80",
        "repeat one", "repeat all", "repeat off", "shuffle on", "shuffle off",
        "radio on", "radio off",
        "theme purple", "theme blue", "theme green", "theme amber", "theme red",
        "ascii braille", "ascii block", "ascii classic", "ascii ultra",
        "reload cover", "reload lyrics", "scan library", "playlist", "queue",
        "online playlist", "mute", "speed 1.25", "fullscreen", "exit",
    ]

    def __init__(self) -> None:
        self._commands: list[Command] = []

    def register(self, pattern: str, description: str, handler: Handler) -> None:
        self._commands.append(Command(re.compile(pattern, re.IGNORECASE), description, handler))

    def suggestions(self, query: str) -> list[str]:
        query = query.lower().strip()
        if not query:
            return self.STATIC_SUGGESTIONS[:10]
        return [s for s in self.STATIC_SUGGESTIONS if query in s.lower()][:10]

    def execute(self, text: str) -> bool:
        text = text.strip()
        for command in self._commands:
            match = command.pattern.match(text)
            if match:
                command.handler(match)
                return True
        return False

    def help_text(self) -> list[str]:
        return [c.description for c in self._commands]
