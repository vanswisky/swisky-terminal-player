"""
theme_manager.py
=================
Thin runtime wrapper around `theme.THEMES` that lets the rest of the
app ask "what's the current theme" and switch themes by name without
importing `settings_manager` directly (keeps `theme.py` dependency-free).
"""

from __future__ import annotations

from theme import THEMES, Theme, DEFAULT_THEME_NAME


class ThemeManager:
    def __init__(self, initial: str = DEFAULT_THEME_NAME) -> None:
        self._current_name = initial if initial in THEMES else DEFAULT_THEME_NAME

    @property
    def current(self) -> Theme:
        return THEMES[self._current_name]

    @property
    def name(self) -> str:
        return self._current_name

    def set_theme(self, name: str) -> bool:
        if name not in THEMES:
            return False
        self._current_name = name
        return True

    @staticmethod
    def available() -> list[str]:
        return list(THEMES.keys())
