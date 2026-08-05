"""
theme.py
========
Declarative color palettes for the cyberpunk UI. Each `Theme` is a
frozen dataclass of TrueColor hex strings consumed by `rich` styles
throughout `ui.py` and `widgets.py`.

Themes are swapped at runtime via `theme_manager.py`; nothing here
depends on application state.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Theme:
    name: str
    accent: str          # primary neon accent (progress bar fill, highlights)
    accent_dim: str       # dimmer variant for secondary emphasis
    background: str
    surface: str          # panel backgrounds
    border: str
    text_primary: str
    text_secondary: str
    text_muted: str
    success: str
    warning: str
    danger: str


PURPLE = Theme(
    name="purple",
    accent="#b05cff",
    accent_dim="#7d3fb8",
    background="#0a0a0f",
    surface="#121018",
    border="#3a2a52",
    text_primary="#f2eaff",
    text_secondary="#b8a9d9",
    text_muted="#5c5470",
    success="#5cffb0",
    warning="#ffd75c",
    danger="#ff5c7a",
)

BLUE = Theme(
    name="blue",
    accent="#5c9eff",
    accent_dim="#3f6bb8",
    background="#080a0f",
    surface="#0f1420",
    border="#233a52",
    text_primary="#eaf2ff",
    text_secondary="#a9c0d9",
    text_muted="#54607a",
    success="#5cffb0",
    warning="#ffd75c",
    danger="#ff5c7a",
)

GREEN = Theme(
    name="green",
    accent="#5cff8f",
    accent_dim="#3fb86a",
    background="#080f0a",
    surface="#0f1a13",
    border="#234f2e",
    text_primary="#eaffef",
    text_secondary="#a9d9b8",
    text_muted="#4f7a5c",
    success="#5cffb0",
    warning="#ffd75c",
    danger="#ff5c7a",
)

AMBER = Theme(
    name="amber",
    accent="#ffb05c",
    accent_dim="#b87d3f",
    background="#0f0c08",
    surface="#1a150f",
    border="#523a23",
    text_primary="#fff2ea",
    text_secondary="#d9c0a9",
    text_muted="#7a6754",
    success="#5cffb0",
    warning="#ffd75c",
    danger="#ff5c7a",
)

RED = Theme(
    name="red",
    accent="#ff5c7a",
    accent_dim="#b83f56",
    background="#0f0808",
    surface="#1a0f0f",
    border="#522323",
    text_primary="#fff0ea",
    text_secondary="#d9a9a9",
    text_muted="#7a5454",
    success="#5cffb0",
    warning="#ffd75c",
    danger="#ff5c7a",
)

NEUTRAL = Theme(
    name="Charcoal/Zinc",
    accent="#FAFAFA",
    accent_dim="#A1A1AA",
    background="#09090B",
    surface="#18181B",
    border="#27272A",
    text_primary="#F4F4F5",
    text_secondary="#D4D4D8",
    text_muted="#71717A",
    success="#5cffb0",
    warning="#ffd75c",
    danger="#ff5c7a",
)

THEMES: dict[str, Theme] = {
    "purple": PURPLE,
    "blue": BLUE,
    "green": GREEN,
    "amber": AMBER,
    "red": RED,
    "Charcoal/Zinc": NEUTRAL
}

DEFAULT_THEME_NAME = "purple"
