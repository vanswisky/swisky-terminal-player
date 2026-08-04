"""
constants.py
============
Static, non-configurable values shared across the application:
character sets for ASCII rendering, supported audio formats, default
paths, and key-binding identifiers.

Nothing in this module should change at runtime — user-tunable values
live in `config.py` / `settings_manager.py` instead.
"""

from __future__ import annotations

from pathlib import Path

# --------------------------------------------------------------------------
# Filesystem layout
# --------------------------------------------------------------------------

APP_NAME = "Swisky Terminal Player"
APP_SLUG = "swisky-terminal-player"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = PROJECT_ROOT / "assets"
MUSIC_DIR = ASSETS_DIR / "music"
COVERS_DIR = ASSETS_DIR / "covers"
LYRICS_DIR = ASSETS_DIR / "lyrics"
CACHE_DIR = ASSETS_DIR / "cache"
PLAYLISTS_DIR = ASSETS_DIR / "playlists"
CONFIG_PATH = PROJECT_ROOT / "config.json"

# --------------------------------------------------------------------------
# Supported audio formats
# --------------------------------------------------------------------------

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".mp3", ".flac", ".wav", ".ogg", ".aac", ".opus", ".m4a", ".aiff", ".aif"}
)

# --------------------------------------------------------------------------
# ASCII character ramps (dark -> light)
# --------------------------------------------------------------------------

# Classic dense-to-sparse ramp, ordered from "most ink" to "least ink".
ASCII_CLASSIC_RAMP = r"@$B%8&WM#*oahkbdpqwmZO0QLCJYXzcvunxrjft/\|()1{}[]?-_+~<>i!lI;:,\"^`'. "[::-1]

# Unicode block-shade characters, sparse -> dense.
ASCII_BLOCK_RAMP = " ░▒▓█"

# Braille dot-count ramp used only for coarse luminance fallback;
# true braille mode builds cells bit-by-bit (see ascii_renderer.py).
BRAILLE_BASE = 0x2800

# Braille dot bit layout (row, col) -> bit index, per the Unicode
# braille pattern block specification.
BRAILLE_DOT_MAP = (
    (0, 0, 0x01), (1, 0, 0x02), (2, 0, 0x04), (0, 1, 0x08),
    (1, 1, 0x10), (2, 1, 0x20), (3, 0, 0x40), (3, 1, 0x80),
)

# --------------------------------------------------------------------------
# Visualizer
# --------------------------------------------------------------------------

VISUALIZER_BARS_RAMP = " ▁▂▃▄▅▆▇█"
FFT_BAND_COUNT_DEFAULT = 48

# --------------------------------------------------------------------------
# Key identifiers (logical names, mapped to raw sequences in keyboard_handler)
# --------------------------------------------------------------------------

KEY_PLAY_PAUSE = "SPACE"
KEY_SEEK_BACK = "LEFT"
KEY_SEEK_FWD = "RIGHT"
KEY_SEEK_BACK_BIG = "CTRL_LEFT"
KEY_SEEK_FWD_BIG = "CTRL_RIGHT"
KEY_VOL_UP = "UP"
KEY_VOL_DOWN = "DOWN"
KEY_NEXT = "n"
KEY_PREV = "p"
KEY_REPEAT = "r"
KEY_SHUFFLE = "s"
KEY_LYRICS = "l"
KEY_ASCII_MODE = "a"
KEY_RELOAD_COVER = "c"
KEY_VISUALIZER = "v"
KEY_FULLSCREEN = "f"
KEY_COMMAND_PALETTE = "CTRL_P"
KEY_MENU = "ESC"
KEY_QUIT = "q"

FRAME_TARGET_FPS_UI = 30
FRAME_TARGET_FPS_VISUALIZER = 60
