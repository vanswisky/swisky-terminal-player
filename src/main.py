"""
main.py
=======
Entry point. Wires every subsystem together in dependency order and
starts the UI render loop. Run with:

    python src/main.py [music_dir ...]

If no directories are given, falls back to `assets/music/` and any
`library_paths` saved in `config.json`.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ascii_cache import AsciiCache
from ascii_renderer import AsciiRenderer
from constants import CACHE_DIR, LYRICS_DIR, MUSIC_DIR, PROJECT_ROOT
from lyrics_manager import LyricsManager
from playlist_manager import PlaylistManager
from player import Player
from scanner import LibraryScanner
from settings_manager import SettingsManager
from theme_manager import ThemeManager
from ui import App
from utils import ensure_dirs
from visualizer import SpectrumAnalyzer

logging.basicConfig(
    filename=str(PROJECT_ROOT / "swisky.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("main")


def _detach_stdin() -> None:
    """Point fd 0 at /dev/null before mpv is constructed.

    `keyboard_handler.py` reads keys from /dev/tty directly, not stdin,
    so this has no effect on our own input handling. What it does do is
    guarantee that if libmpv's own terminal-input feature ever reads
    fd 0 (e.g. `terminal=False` not being honored on some mpv build),
    there's genuinely nothing there for it to steal — instead of racing
    our keyboard thread for the same bytes.
    """
    try:
        devnull_fd = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull_fd, 0)
        os.close(devnull_fd)
    except OSError as exc:
        logger.warning("Could not detach stdin from the terminal: %s", exc)


def main() -> None:
    _detach_stdin()
    ensure_dirs(MUSIC_DIR, LYRICS_DIR, CACHE_DIR)

    settings = SettingsManager()
    config = settings.config

    cli_dirs = sys.argv[1:]
    music_dirs = cli_dirs or config.library_paths or [str(MUSIC_DIR)]
    config.library_paths = music_dirs
    settings.save()

    theme_mgr = ThemeManager(config.theme)

    lyrics = LyricsManager(offset_ms=config.lyrics.offset_ms)
    player = Player(config.playback, lyrics)

    ascii_renderer = AsciiRenderer(config.ascii)
    ascii_cache = AsciiCache(ascii_renderer)

    spectrum = SpectrumAnalyzer(config.visualizer)

    playlist = PlaylistManager()
    scanner = LibraryScanner(music_dirs)

    logger.info("Scanning music library: %s", music_dirs)
    tracks = scanner.scan_all()
    playlist.set_library(tracks)
    scanner.on_change(lambda: playlist.set_library(scanner.scan_all()))
    scanner.start_watching()

    if tracks:
        player.load_queue(tracks, start_index=0)
        player.pause()  # start paused; user presses SPACE to begin

    app = App(
        settings=settings,
        player=player,
        ascii_cache=ascii_cache,
        spectrum=spectrum,
        lyrics=lyrics,
        playlist=playlist,
        scanner=scanner,
        theme_mgr=theme_mgr,
    )

    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        settings.save()


if __name__ == "__main__":
    main()
