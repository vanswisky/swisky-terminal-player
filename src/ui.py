"""
ui.py
=====
Owns the `rich.Live` render loop and terminal layout. This is the
integration point: it wires keyboard/mouse input to `player.py`,
asks `ascii_cache.py` for the current cover's ASCII frame, asks
`visualizer.py` for the current spectrum, and asks `widgets.py` to
draw all of it into a `Layout` matching the spec:

    ┌───────────────────────────────────────────┐
    │                 SEARCH BAR                  │
    ├───────────────────┬─────────────────────────┤
    │                   │  NOW PLAYING / LYRICS    │
    │   ASCII ALBUM ART │  (enlarged, no dummy     │
    │                   │   "features" panel)      │
    ├───────────────────┴─────────────────────────┤
    │   REALTIME AUDIO SPECTRUM (full width) — only │
    │   present when the visualizer is turned ON    │
    ├───────────────────────────────────────────────┤
    │  VOL│PREV│PLAY│NEXT│REPEAT│SHUFFLE│QUEUE│...      │
    └───────────────────────────────────────────────┘

The spectrum row (see `_visualizer_height`) collapses to 0 rows
whenever `config.visualizer.enabled` is off (toggle with the `v` key
or from Settings), handing that space back to the album art / now
playing area so the UI feels bigger with it off rather than leaving
an empty panel behind. Cover art is re-rendered at the new panel size
whenever that happens (see the `current_size` cache key in
`_build_layout`) so the ASCII art's own aspect ratio stays correct
instead of looking stretched/squashed.

Screen modes (mutually exclusive overlays): NORMAL, QUEUE, SETTINGS,
COMMAND_PALETTE, SEARCH. Only one owns keyboard focus at a time.
"""

from __future__ import annotations

import enum
import logging
import threading
import time

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

import online_source
import widgets
from ascii_cache import AsciiCache
from command_palette import CommandPalette
from config import AppConfig, AsciiRenderMode, RepeatMode
from keyboard_handler import KeyboardHandler
from lyrics_manager import LyricsManager
from mouse_handler import MouseAction, disable_mouse_reporting, enable_mouse_reporting, parse_mouse_sequence
from playlist_manager import PlaylistManager
from player import Player
from scanner import LibraryScanner
from settings_items import SETTINGS_ITEMS
from settings_manager import SettingsManager
from theme_manager import ThemeManager
from visualizer import SpectrumAnalyzer

logger = logging.getLogger(__name__)

# Must match the column order produced by widgets.render_control_bar exactly.
# ("seek" was removed — clicking it never did anything; the progress bar
# itself is the click-to-seek control, handled separately below.)
CONTROL_BAR_SEGMENTS = (
    "volume", "previous", "play_pause", "next",
    "repeat", "shuffle", "queue", "settings", "exit",
)

# Horizontal chrome the control bar's Panel consumes before its content
# starts: a 1-char border plus Panel's default (0, 1) padding, on each
# side. Both `_control_bar_geometry` (hit-testing) and `_build_layout`
# (rendering) derive their column widths from this same constant, so
# a click and the button it visually lands on can't drift apart the
# way they used to (the old hit-test math only accounted for the
# border, not the padding, and started counting one column too early).
_PANEL_H_CHROME = 4
_PANEL_CONTENT_LEFT = 3  # 1-indexed column of the first interior character


class ScreenMode(enum.Enum):
    NORMAL = enum.auto()
    QUEUE = enum.auto()
    SETTINGS = enum.auto()
    COMMAND_PALETTE = enum.auto()
    SEARCH = enum.auto()


class App:
    # Fixed row heights for the non-body layout regions. Defined once
    # here and reused everywhere (Layout construction, mouse hit-testing,
    # cover-art panel sizing) — these three numbers drifting out of sync
    # with each other is exactly what broke mouse seeking and ASCII
    # art scaling before.
    HEADER_H = 3
    VISUALIZER_H = 10
    CONTROLS_H = 3

    def __init__(
        self,
        settings: SettingsManager,
        player: Player,
        ascii_cache: AsciiCache,
        spectrum: SpectrumAnalyzer,
        lyrics: LyricsManager,
        playlist: PlaylistManager,
        scanner: LibraryScanner,
        theme_mgr: ThemeManager,
    ) -> None:
        self.settings = settings
        self.config: AppConfig = settings.config
        self.player = player
        self.ascii_cache = ascii_cache
        self.spectrum = spectrum
        self.lyrics = lyrics
        self.playlist = playlist
        self.scanner = scanner
        self.theme_mgr = theme_mgr

        self.console = Console()
        self.keyboard = KeyboardHandler()
        self.mode = ScreenMode.NORMAL
        self.running = True
        self.palette_input = ""
        self.queue_cursor = 0
        self.queue_scroll = 0
        self.queue_visible_rows = 12
        self.settings_cursor = 0
        self._last_cover: str | None = None
        # (width, height, visualizer_enabled) snapshot — see the note
        # near `_last_art_size`'s assignment below for why the third
        # element matters.
        self._last_art_size: tuple[int, int, bool] | None = None
        self._ascii_frame = None

        # -- Online (iTunes search, YouTube audio) screen state ------
        # A single incrementing generation counter gates every
        # background search/resolve worker below: bumping it makes any
        # in-flight worker's result get silently dropped once it lands
        # (see `_poll_online_search`). Same pattern as the generation
        # gate in lyrics_manager.py, for the same reason — a slow
        # network response for a query/track the user has since moved
        # past must not clobber whatever's on screen or playing now.
        self.search_input = ""
        self.search_results: list = []
        self.search_cursor = 0
        self.search_status = ""
        self._search_lock = threading.Lock()
        self._search_generation = 0
        self._pending_search_result = None
        self._pending_resolve = None

        # Hit regions for mouse support, recomputed each frame.
        self._progress_row: int | None = None
        self._progress_cols: tuple[int, int] | None = None
        self._control_row: int | None = None

        self.command_palette = CommandPalette()
        self._register_commands()

        self.player.on_track_changed(self._on_track_changed)
        # main.py loads the initial queue before this App (and this
        # listener) exists, so the very first "track changed" event fires
        # into an empty listener list — the spectrum analyzer never gets
        # told to decode that track. Sync explicitly for whatever's
        # already loaded so this works regardless of construction order.
        self._on_track_changed(self.player.current_track())

    # -- command palette wiring --------------------------------------

    def _register_commands(self) -> None:
        cp = self.command_palette
        cp.register(r"^play$", "play — resume playback", lambda m: self.player.resume())
        cp.register(r"^pause$", "pause — pause playback", lambda m: self.player.pause())
        cp.register(r"^next$", "next — skip to next track", lambda m: self.player.next())
        cp.register(r"^previous$", "previous — go to previous track", lambda m: self.player.previous())
        cp.register(r"^seek (\d{1,2}):(\d{2})$", "seek MM:SS — jump to position",
                    lambda m: self.player.seek_absolute(int(m.group(1)) * 60 + int(m.group(2))))
        cp.register(r"^volume (\d{1,3})$", "volume N — set volume",
                    lambda m: self.player.set_volume(int(m.group(1))))
        cp.register(r"^mute$", "mute — toggle mute", lambda m: self.player.toggle_mute())
        cp.register(r"^speed ([\d.]+)$", "speed N — set playback speed",
                    lambda m: self.player.set_speed(float(m.group(1))))
        cp.register(r"^repeat (one|all|off)$", "repeat one|all|off",
                    lambda m: setattr(self.config.playback, "repeat", RepeatMode(m.group(1))))
        cp.register(r"^shuffle (on|off)$", "shuffle on|off",
                    lambda m: setattr(self.config.playback, "shuffle", m.group(1) == "on"))
        cp.register(r"^theme (\w+)$", "theme NAME — switch color theme",
                    lambda m: self._set_theme(m.group(1)))
        cp.register(r"^ascii (classic|block|braille)$", "ascii MODE — switch ASCII renderer",
                    lambda m: self._set_ascii_mode(m.group(1)))
        cp.register(r"^ascii ultra$", "ascii ultra — max ASCII quality",
                    lambda m: self._set_ascii_quality("ultra"))
        cp.register(r"^reload cover$", "reload cover — force re-render ASCII art",
                    lambda m: self._reload_cover(force=True))
        cp.register(r"^reload lyrics$", "reload lyrics — reload .lrc for current track",
                    lambda m: self.lyrics.reload())
        cp.register(r"^scan library$", "scan library — rescan music folders",
                    lambda m: self._rescan_library())
        cp.register(r"^playlist$", "playlist — open playlist browser", lambda m: self._set_mode(ScreenMode.QUEUE))
        cp.register(r"^queue$", "queue — open queue view", lambda m: self._set_mode(ScreenMode.QUEUE))
        cp.register(r"^online (.+)$", "online QUERY — search for a track and open results",
                    lambda m: self._open_search(prefill=m.group(1)))
        cp.register(r"^exit$", "exit — quit the app", lambda m: self._quit())

    # -- lifecycle ---------------------------------------------------------

    def _on_track_changed(self, track) -> None:
        if track is not None:
            self.spectrum.load_async(track.path)
            self._last_cover = None  # force ASCII re-render on next frame

    def _set_theme(self, name: str) -> None:
        if self.theme_mgr.set_theme(name):
            self.config.theme = name
            self.settings.save()

    def _set_ascii_mode(self, mode: str) -> None:
        self.config.ascii.mode = AsciiRenderMode(mode)
        self.ascii_cache.update_config(self.config.ascii)
        self._last_cover = None
        self.settings.save()

    def _set_ascii_quality(self, quality: str) -> None:
        from config import AsciiQuality
        self.config.ascii.quality = AsciiQuality(quality)
        self.ascii_cache.update_config(self.config.ascii)
        self._last_cover = None
        self.settings.save()

    def _reload_cover(self, force: bool = False) -> None:
        self._last_cover = None

    def _rescan_library(self) -> None:
        tracks = self.scanner.scan_all()
        self.playlist.set_library(tracks)

    def _set_mode(self, mode: ScreenMode) -> None:
        if mode == ScreenMode.QUEUE:
            # Land on whatever's currently playing rather than always
            # snapping back to the top of the list.
            self.queue_cursor = max(0, self.player.queue.cursor)
            self._sync_queue_scroll()
        self.mode = mode

    def _sync_queue_scroll(self) -> None:
        """Keeps `queue_scroll` in a window that contains `queue_cursor`.
        Without this, moving the cursor past the first page of a long
        queue moved state nobody could see — indistinguishable from the
        arrow keys doing nothing at all.
        """
        rows = self.queue_visible_rows
        if self.queue_cursor < self.queue_scroll:
            self.queue_scroll = self.queue_cursor
        elif self.queue_cursor >= self.queue_scroll + rows:
            self.queue_scroll = self.queue_cursor - rows + 1
        self.queue_scroll = max(0, self.queue_scroll)

    # -- online (iTunes search, YouTube audio) search --------------------
    #
    # Two network-bound steps (see online_source.py's module docstring
    # for why they're split): a cheap `search()` against iTunes to list
    # candidates, then a `resolve()` — only for whichever one the user
    # actually picks — that finds a matching YouTube stream to actually
    # play. Both run on
    # background threads; `_poll_online_search`, called once per frame
    # from `run()` (same pattern as `player.poll()`), is the only place
    # that touches `self.search_*` state from the results, so nothing
    # here ever has to worry about a render happening mid-mutation.

    def _open_search(self, prefill: str = "") -> None:
        self.mode = ScreenMode.SEARCH
        self.search_input = prefill
        self.search_results = []
        self.search_cursor = 0
        self.search_status = "" if self.config.online.enabled else "Online search is off — enable it in Settings."
        if prefill.strip() and self.config.online.enabled:
            self._start_online_search(prefill.strip())

    def _start_online_search(self, query: str) -> None:
        if not self.config.online.enabled:
            self.search_status = "Online search is off — enable it in Settings."
            return
        self.search_status = f"Searching '{query}'…"
        self.search_results = []
        self.search_cursor = 0
        generation = self._bump_search_generation()
        threading.Thread(
            target=self._search_worker, args=(query, generation), daemon=True
        ).start()

    def _search_worker(self, query: str, generation: int) -> None:
        try:
            results = online_source.search(query, limit=self.config.online.search_results)
            payload = ("ok", results)
        except online_source.OnlineSourceError as exc:
            payload = ("error", str(exc))
        with self._search_lock:
            if generation == self._search_generation:
                self._pending_search_result = payload

    def _resolve_and_play_online(self, result) -> None:
        self._resolve_online(result, mode="play")

    def _resolve_and_enqueue_online(self, result) -> None:
        self._resolve_online(result, mode="enqueue")

    def _resolve_online(self, result, mode: str) -> None:
        self.search_status = f"Loading '{result.title}'…"
        generation = self._bump_search_generation()
        threading.Thread(
            target=self._resolve_worker, args=(result, mode, generation), daemon=True
        ).start()

    def _resolve_worker(self, result, mode: str, generation: int) -> None:
        try:
            track = online_source.resolve(result)
            payload = ("ok", mode, track)
        except online_source.OnlineSourceError as exc:
            payload = ("error", mode, str(exc))
        with self._search_lock:
            if generation == self._search_generation:
                self._pending_resolve = payload

    def _bump_search_generation(self) -> int:
        with self._search_lock:
            self._search_generation += 1
            self._pending_search_result = None
            self._pending_resolve = None
            return self._search_generation

    def _cancel_search(self) -> None:
        self._bump_search_generation()
        self.search_status = ""

    def _poll_online_search(self) -> None:
        """Drains whatever a background search/resolve worker left
        behind onto the main thread. Called once per frame from
        `run()`, same pattern as `player.poll()` for track-end — never
        touch `self.search_*` state directly from a worker thread.
        """
        with self._search_lock:
            pending_search = self._pending_search_result
            self._pending_search_result = None
            pending_resolve = self._pending_resolve
            self._pending_resolve = None

        if pending_search is not None:
            kind, payload = pending_search
            if kind == "ok":
                self.search_results = payload
                self.search_cursor = 0
                self.search_status = ""
            else:
                self.search_status = payload

        if pending_resolve is not None:
            kind, mode, payload = pending_resolve
            if kind == "ok":
                track = payload
                if mode == "play":
                    self.player.play_online_next(track)
                    self.mode = ScreenMode.NORMAL  # jump to now-playing
                else:
                    self.player.queue.add_next(track)
                self.search_status = f"Added '{track.title}' to queue." if mode == "enqueue" else ""
            else:
                self.search_status = payload

    def _quit(self) -> None:
        self.running = False

    # -- main loop -----------------------------------------------------

    def run(self) -> None:
        self.keyboard.start()
        if self.config.mouse_enabled:
            enable_mouse_reporting()

        frame_interval = 1.0 / max(1, self.config.ui_fps)
        try:
            with Live(console=self.console, screen=True, auto_refresh=False, transient=False) as live:
                while self.running:
                    start = time.monotonic()
                    self.player.poll()  # advance queue on natural track end
                    self._poll_online_search()  # drain background search/resolve results
                    self._handle_input()
                    layout = self._build_layout()
                    live.update(layout, refresh=True)
                    elapsed = time.monotonic() - start
                    time.sleep(max(0.0, frame_interval - elapsed))
        finally:
            if self.config.mouse_enabled:
                disable_mouse_reporting()
            self.keyboard.stop()
            self.player.shutdown()
            self.scanner.stop_watching()

    # -- input handling --------------------------------------------------

    def _handle_input(self) -> None:
        for key in self.keyboard.drain():
            if key.startswith("MOUSE:"):
                self._handle_mouse(key)
                continue
            if self.mode == ScreenMode.COMMAND_PALETTE:
                self._handle_palette_key(key)
            elif self.mode == ScreenMode.QUEUE:
                self._handle_queue_key(key)
            elif self.mode == ScreenMode.SETTINGS:
                self._handle_settings_key(key)
            elif self.mode == ScreenMode.SEARCH:
                self._handle_search_key(key)
            else:
                self._handle_normal_key(key)

    def _handle_normal_key(self, key: str) -> None:
        cfg = self.config
        if key == "SPACE":
            self.player.toggle_play_pause()
        elif key == "LEFT":
            self.player.seek_relative(-5)
        elif key == "RIGHT":
            self.player.seek_relative(5)
        elif key == "CTRL_LEFT":
            self.player.seek_relative(-30)
        elif key == "CTRL_RIGHT":
            self.player.seek_relative(30)
        elif key == "UP":
            self.player.volume_up()
        elif key == "DOWN":
            self.player.volume_down()
        elif key == "n":
            self.player.next()
        elif key == "p":
            self.player.previous()
        elif key == "r":
            self.player.cycle_repeat()
        elif key == "s":
            self.player.toggle_shuffle()
        elif key == "l":
            cfg.lyrics.enabled = not cfg.lyrics.enabled
        elif key == "a":
            order = [AsciiRenderMode.CLASSIC, AsciiRenderMode.BLOCK, AsciiRenderMode.BRAILLE]
            idx = (order.index(cfg.ascii.mode) + 1) % len(order)
            self._set_ascii_mode(order[idx].value)
        elif key == "c":
            self._reload_cover(force=True)
        elif key == "v":
            cfg.visualizer.enabled = not cfg.visualizer.enabled
        elif key == "o":
            self._open_search()
        elif key == "f":
            pass  # fullscreen is inherent to `Live(screen=True)`
        elif key == "CTRL_P":
            self.mode = ScreenMode.COMMAND_PALETTE
            self.palette_input = ""
        elif key == "ESC":
            self.mode = ScreenMode.SETTINGS
        elif key == "q" or key == "QUIT":
            self._quit()

    def _handle_palette_key(self, key: str) -> None:
        if key == "ESC":
            self.mode = ScreenMode.NORMAL
        elif key == "ENTER":
            self.command_palette.execute(self.palette_input)
            self.mode = ScreenMode.NORMAL
        elif key == "BACKSPACE":
            self.palette_input = self.palette_input[:-1]
        elif key == "SPACE":
            # keyboard_handler.py reports the space bar as the logical
            # token "SPACE" (5 chars), not a literal " " (1 char) —
            # needed so NORMAL mode can bind it to play/pause without
            # colliding with printable-character input. But that means
            # the `len(key) == 1` branch below never matches a space
            # bar press, so multi-word input (e.g. "online rex orange
            # county") silently dropped every space and produced
            # "onlinerexorangecounty". This branch is what was missing.
            self.palette_input += " "
        elif len(key) == 1:
            self.palette_input += key

    def _handle_queue_key(self, key: str) -> None:
        items = self.player.queue.items
        if key == "ESC":
            self.mode = ScreenMode.NORMAL
        elif key == "UP":
            self.queue_cursor = max(0, self.queue_cursor - 1)
            self._sync_queue_scroll()
        elif key == "DOWN":
            self.queue_cursor = min(max(0, len(items) - 1), self.queue_cursor + 1)
            self._sync_queue_scroll()
        elif key == "ENTER":
            self.player.play_index(self.queue_cursor)
        elif key == "n":
            self.player.queue.move_down(self.queue_cursor)
        elif key == "p":
            self.player.queue.move_up(self.queue_cursor)
        elif key == "d" or key == "BACKSPACE":
            self.player.queue.remove(self.queue_cursor)
            self.queue_cursor = min(self.queue_cursor, max(0, len(self.player.queue.items) - 1))
            self._sync_queue_scroll()

    def _handle_search_key(self, key: str) -> None:
        """Text input and results browsing share one screen and one key
        handler. While there are no results yet, typed characters build
        `search_input` (Enter searches). Once results are showing,
        UP/DOWN move the selection, Enter plays it, "a" adds it to the
        queue without switching playback, and typing again clears the
        list and starts a fresh query — the same "keep typing to search
        again" feel as a fuzzy-finder.
        """
        if key == "ESC":
            self._cancel_search()
            self.mode = ScreenMode.NORMAL
        elif key == "UP" and self.search_results:
            self.search_cursor = max(0, self.search_cursor - 1)
        elif key == "DOWN" and self.search_results:
            self.search_cursor = min(len(self.search_results) - 1, self.search_cursor + 1)
        elif key == "ENTER":
            if self.search_results:
                self._resolve_and_play_online(self.search_results[self.search_cursor])
            elif self.search_input.strip():
                self._start_online_search(self.search_input.strip())
        elif key == "a" and self.search_results:
            self._resolve_and_enqueue_online(self.search_results[self.search_cursor])
        elif key == "BACKSPACE":
            if self.search_results:
                self.search_results = []
                self.search_cursor = 0
                self.search_status = ""
            else:
                self.search_input = self.search_input[:-1]
        elif key == "SPACE":
            # See _handle_palette_key for why "SPACE" (not a literal
            # " ") is what arrives here — without this branch, every
            # space in a query got silently dropped, so "rex orange
            # county" typed as "rexorangecounty" and search results
            # for actual song titles/artists (almost always more than
            # one word) never matched anything.
            if self.search_results:
                self.search_results = []
                self.search_cursor = 0
                self.search_status = ""
                self.search_input = " "
            else:
                self.search_input += " "
        elif len(key) == 1:
            if self.search_results:
                self.search_results = []
                self.search_cursor = 0
                self.search_status = ""
                self.search_input = key
            else:
                self.search_input += key

    def _handle_settings_key(self, key: str) -> None:
        if key == "ESC":
            self.mode = ScreenMode.NORMAL
            self.settings.save()
        elif key == "UP":
            self.settings_cursor = (self.settings_cursor - 1) % len(SETTINGS_ITEMS)
        elif key == "DOWN":
            self.settings_cursor = (self.settings_cursor + 1) % len(SETTINGS_ITEMS)
        elif key in ("LEFT", "RIGHT"):
            self._change_setting(1 if key == "RIGHT" else -1)

    def _change_setting(self, direction: int) -> None:
        item = SETTINGS_ITEMS[self.settings_cursor]
        item.change(self.config, direction)
        if item.changes_theme:
            self.theme_mgr.set_theme(self.config.theme)
        if item.invalidates_cover:
            self.ascii_cache.update_config(self.config.ascii)
            self._last_cover = None
        if item.key == "lyrics_auto_fetch":
            # LyricsManager reads its own `auto_fetch` attribute rather
            # than the config live, so this has to be pushed explicitly
            # — otherwise the toggle would only take effect after a
            # restart, same trap `playback_speed` avoids below.
            self.lyrics.auto_fetch = self.config.lyrics.auto_fetch
        # Playback-affecting settings should take effect immediately,
        # not just once the Settings screen is closed.
        self.player.set_speed(self.config.playback.speed)
        self.settings.save()

    def _handle_mouse(self, raw: str) -> None:
        event = parse_mouse_sequence(raw)
        if event is None:
            return

        if event.action == MouseAction.SCROLL_UP:
            self.player.volume_up(2)
            return
        if event.action == MouseAction.SCROLL_DOWN:
            self.player.volume_down(2)
            return
        if event.action != MouseAction.PRESS:
            return

        # Progress bar seek (only meaningful in NORMAL mode; overlays own
        # the whole screen while open).
        if self.mode == ScreenMode.NORMAL and self._progress_row is not None and event.row == self._progress_row:
            lo, hi = self._progress_cols
            if lo <= event.column <= hi:
                fraction = (event.column - lo) / max(1, hi - lo)
                self.player.seek_to_fraction(fraction)
                return

        # Control bar buttons. Still live during SEARCH — the control
        # bar stays visible there (see `_build_layout`) so playback of
        # whatever's already playing can keep being controlled while
        # browsing search results, rather than going dead/decorative.
        if self.mode in (ScreenMode.NORMAL, ScreenMode.SEARCH) and self._control_row is not None \
                and event.row == self._control_row:
            segment = self._control_bar_segment_at(event.column)
            if segment:
                self._dispatch_control_bar_click(segment)

    def _control_bar_geometry(self) -> tuple[int, list[int]]:
        """Returns `(content_left, column_widths)` — the exact same
        numbers `_build_layout` hands to `widgets.render_control_bar`
        for drawing, so a click and the button rendered under it can
        never drift apart from each other.
        """
        n = len(CONTROL_BAR_SEGMENTS)
        usable = max(n, self.console.size.width - _PANEL_H_CHROME)
        base = usable // n
        remainder = usable - base * n
        # Give the leftover columns (from integer division) to the
        # first few segments, one extra char each, so the widths sum
        # to exactly `usable` instead of leaving a dead strip on the
        # right that both drawing and hit-testing have to agree to
        # ignore.
        widths = [base + (1 if i < remainder else 0) for i in range(n)]
        return _PANEL_CONTENT_LEFT, widths

    def _control_bar_segment_at(self, column: int) -> str | None:
        content_left, widths = self._control_bar_geometry()
        pos = column - content_left
        if pos < 0:
            return None
        cursor = 0
        for segment, width in zip(CONTROL_BAR_SEGMENTS, widths):
            if pos < cursor + width:
                return segment
            cursor += width
        return None

    def _dispatch_control_bar_click(self, segment: str) -> None:
        actions = {
            "volume": lambda: self.player.toggle_mute(),
            "previous": lambda: self.player.previous(),
            "play_pause": lambda: self.player.toggle_play_pause(),
            "next": lambda: self.player.next(),
            "repeat": lambda: self.player.cycle_repeat(),
            "shuffle": lambda: self.player.toggle_shuffle(),
            "queue": lambda: self._set_mode(ScreenMode.QUEUE),
            "settings": lambda: self._set_mode(ScreenMode.SETTINGS),
            "exit": lambda: self._quit(),
        }
        action = actions.get(segment)
        if action:
            action()

    # -- layout building -----------------------------------------------

    def _visualizer_height(self) -> int:
        """Effective row-height of the spectrum region: `VISUALIZER_H`
        while the visualizer is on, or 0 while it's off. Collapsing to
        0 (rather than just leaving it blank) is what actually hands
        those rows back to the art/info area above it — `layout["body"]`
        has `ratio=1` so it expands to fill whatever the visualizer
        region doesn't take.
        """
        return self.VISUALIZER_H if self.config.visualizer.enabled else 0

    def _body_height(self) -> int:
        """Rows available to the `body` row (art + info) once the fixed
        header/visualizer/controls regions are subtracted. This is the
        same number `Layout`'s `ratio=1` resolves `body` to internally
        — but a `rich.panel.Panel` does NOT auto-stretch to match its
        `Layout` region (it sizes to its own content and leaves the
        rest of the region unpainted), so the cover art panel needs
        this number handed to it explicitly (`height=`) to actually
        reach all the way down instead of floating as a short box with
        dead, transparent-looking space underneath it.
        """
        console_h = self.console.size.height
        return max(1, console_h - (self.HEADER_H + self._visualizer_height() + self.CONTROLS_H))

    def _build_layout(self) -> Layout:
        theme = self.theme_mgr.current
        state = self.player.snapshot()
        track = self.player.current_track()
        body_h = self._body_height()

        # The visualizer's on/off state is part of the cache key here,
        # not just console size: toggling it changes the art panel's
        # available height (see `_visualizer_height`/`_art_columns`),
        # so the ASCII frame has to be re-rendered at the new column/row
        # count too — otherwise the old frame (fitted for the old,
        # shorter or taller panel) gets stretched into the new panel
        # shape by the terminal, which is what made the album border
        # look "lonjong" (oval) whenever the spectrum was toggled.
        current_size = (self.console.size.width, self.console.size.height, self.config.visualizer.enabled)
        size_changed = current_size != self._last_art_size
        if track and track.cover_path and (track.cover_path != self._last_cover or size_changed):
            self._ascii_frame = self.ascii_cache.get(
                track.cover_path, columns=self._art_columns(track.cover_path)
            )
            self._last_cover = track.cover_path
            self._last_art_size = current_size
        elif track is None:
            self._ascii_frame = None

        # NOTE: rich.layout's fixed-size resolver treats `size=0` as
        # falsy and silently falls back to ratio-based sizing for that
        # row (`_ratio.py` does `edge.size or None`) — passing
        # `size=self._visualizer_height()` here when it's 0 doesn't
        # "collapse" the visualizer row at all, it makes Rich split the
        # remaining height 50/50 between "body" and "visualizer" (both
        # end up ratio=1), which is exactly what was cutting the cover
        # art / lyrics panel roughly in half whenever the visualizer was
        # turned off. The only reliable way to actually zero out a row
        # is to not create it in the first place.
        visualizer_enabled = self.config.visualizer.enabled
        rows = [
            Layout(name="header", size=self.HEADER_H),
            Layout(name="body", ratio=1),
        ]
        if visualizer_enabled:
            rows.append(Layout(name="visualizer", size=self.VISUALIZER_H))
        rows.append(Layout(name="controls", size=self.CONTROLS_H))

        layout = Layout()
        layout.split_column(*rows)
        layout["body"].split_row(
            Layout(name="art", ratio=1),
            Layout(name="info", ratio=2),
        )

        layout["header"].update(self._render_header(theme))
        layout["art"].update(widgets.render_ascii_art(self._ascii_frame, theme, height=body_h))

        now_playing = widgets.render_now_playing(track, theme)
        progress_bar = widgets.render_progress_bar(state.position, state.duration, theme)

        if self.mode == ScreenMode.SEARCH:
            # Header is doing the input box now (see _render_header),
            # so this panel only needs to carry status/results — art,
            # visualizer and controls all stay put underneath so
            # whatever's already playing keeps showing/playing while
            # you search for the next thing.
            layout["info"].update(self._render_search_results_panel(theme))
        elif self.config.lyrics.enabled and self.config.lyrics.auto_scroll:
            info_body = Group(
                now_playing,
                Text(""),
                progress_bar,
                Text(""),
                widgets.render_lyrics(self.lyrics.state(), state.position, theme),
            )
            # No border here on purpose — per the reference design, the
            # cover art is the only bordered element in the body row;
            # now playing/lyrics just sit directly on the background.
            # `_progress_row`/`_progress_cols` below assume this exact
            # padding (no border) — keep them in sync if this changes.
            layout["info"].update(Padding(info_body, (1, 2)))
        else:
            info_body = Group(now_playing, Text(""), progress_bar)
            layout["info"].update(Padding(info_body, (1, 2)))

        if visualizer_enabled:
            spec_frame = self.spectrum.decay() if state.paused else self.spectrum.analyze_at(state.position)
            vis_width = max(10, self.console.size.width - 4)
            layout["visualizer"].update(widgets.render_visualizer(spec_frame, theme, vis_width, height=8))
        # else: the row simply doesn't exist in this frame's layout (see
        # the note above `rows = [...]`) — nothing to update, and "body"
        # already absorbed the freed-up height via its ratio=1 split.

        _, control_bar_widths = self._control_bar_geometry()
        layout["controls"].update(
            Panel(
                widgets.render_control_bar(
                    theme, state.volume, state.paused, self.config.playback.repeat,
                    self.config.playback.shuffle, self.player.muted, control_bar_widths,
                ),
                border_style=theme.border,
            )
        )

        # -- Hit regions for mouse support -----------------------------
        # Derived from the *fixed* row heights in the layout above
        # (header=3, visualizer=0 or 10 depending on whether it's on,
        # controls=3) plus the known internal structure of the widgets
        # those regions contain. Column split (art:info = 1:2) is
        # approximate since Rich's ratio-resolver doesn't expose exact
        # cell boundaries — close enough for a terminal mouse click.
        HEADER_H, CONTROLS_H = self.HEADER_H, self.CONTROLS_H
        console_w, console_h = self.console.size.width, self.console.size.height

        info_panel_left = int(console_w * 1 / 3) + 1  # art:info ratio is 1:2
        # The `info` column has no border anymore (see `_build_layout` —
        # only the cover art panel is bordered), so content starts right
        # after its `Padding(..., (1, 2))` — no "+1 border" term here.
        info_content_left = info_panel_left + 2

        # Content line offsets inside the NOW PLAYING Group (see
        # widgets.render_now_playing): title, artist, blank, 1 info
        # row (just Album now — Genre/Codec were dropped so there's
        # more vertical room left for lyrics on short terminals),
        # blank, then the progress bar grid.
        PROGRESS_LINE_OFFSET = 7
        self._progress_row = HEADER_H + 1 + PROGRESS_LINE_OFFSET  # +1 padding-top (no border)
        # The bar itself sits after a 6-char time label + 1 space gutter.
        bar_left = info_content_left + 7
        bar_right = console_w - 2 - 6  # panel right padding (no border) + trailing time label
        self._progress_cols = (bar_left, max(bar_left + 1, bar_right))

        # Controls panel occupies the last CONTROLS_H rows; row +1 is the
        # top border, row +2 is where the button text actually renders.
        self._control_row = console_h - CONTROLS_H + 2

        overlay = self._build_overlay(theme)
        if overlay is not None:
            return Layout(overlay)
        return layout

    def _render_header(self, theme) -> Panel:
        """The header is a search bar, always. While `SEARCH` mode is
        open this 3-row strip is the live query input; otherwise it's
        an idle placeholder hinting at the 'o' shortcut that opens it.
        The old static "Swisky~" wordmark panel was removed in favor
        of this so the header stays a functional control instead of
        just branding. Border color switches to accent while typing
        so it's visually obvious the header is now an active input.
        """
        if self.mode == ScreenMode.SEARCH:
            text = Text.assemble(
                ("🔍 ", theme.accent),
                ("Search:  ", f"bold {theme.text_secondary}"),
                (f"{self.search_input}_", f"bold {theme.accent}"),
            )
            return Panel(text, border_style=theme.accent)
        text = Text.assemble(
            ("🔍 ", theme.text_muted),
            ("Search for a track or artist…", theme.text_muted),
            ("  (press 'o')", theme.text_muted),
        )
        return Panel(text, border_style=theme.border)

    def _render_search_results_panel(self, theme):
        """The `info` column while `SEARCH` mode is open. The query
        itself lives in the header now (`_render_header`), so
        `show_query=False` here to avoid showing it twice.

        Borderless like the rest of the `info` column (see
        `_build_layout`) — title/subtitle that a `Panel` would have
        drawn on its border are folded into the body text instead.
        """
        if self.search_results:
            body = widgets.render_search(
                self.search_input, self.search_results, self.search_cursor,
                self.search_status, theme, show_query=False,
            )
            heading = "RESULTS"
            subtitle = "↑/↓ select · Enter play next · a add to queue · type to search again · Esc close"
        elif self.search_status:
            # Covers several states with one line: "Searching '...'",
            # "Loading '...'", "Added '...' to queue", error text, or
            # the "enable it in Settings" nudge — all just status
            # text, not necessarily mid-search, hence the generic title.
            body = Text(self.search_status, style=theme.text_secondary)
            heading = "ONLINE SEARCH"
            subtitle = "Esc close"
        else:
            body = Text("Type above, then press Enter to search.", style=theme.text_muted)
            heading = "ONLINE SEARCH"
            subtitle = "Esc close"
        group = Group(
            Text(heading, style=f"bold {theme.text_muted}"),
            Text(""),
            body,
            Text(""),
            Text(subtitle, style=theme.text_muted),
        )
        return Padding(group, (1, 2))

    def _build_overlay(self, theme):
        if self.mode == ScreenMode.COMMAND_PALETTE:
            suggestions = self.command_palette.suggestions(self.palette_input)
            body = Group(
                Text(f"> {self.palette_input}_", style=f"bold {theme.accent}"),
                Text(""),
                *[Text(s, style=theme.text_secondary) for s in suggestions],
            )
            return Panel(body, title="COMMAND PALETTE", border_style=theme.accent, padding=(1, 2))

        if self.mode == ScreenMode.QUEUE:
            body = widgets.render_queue(
                self.player.queue.items, self.queue_cursor, self.player.queue.cursor, theme,
                max_rows=self.queue_visible_rows, scroll_offset=self.queue_scroll,
            )
            return Panel(body, title="QUEUE", subtitle="↑/↓ move · Enter play · d remove · Esc close",
                         border_style=theme.accent)

        if self.mode == ScreenMode.SETTINGS:
            body = widgets.render_settings(self.config, theme, cursor=self.settings_cursor)
            return Panel(body, title="SETTINGS", subtitle="↑/↓ select · ←/→ change · Esc to close and save",
                         border_style=theme.accent)

        return None

    def _art_columns(self, cover_path: str) -> int:
        from config import ASCII_QUALITY_COLUMNS
        from ascii_renderer import CHAR_ASPECT_COMPENSATION
        from PIL import Image

        base = ASCII_QUALITY_COLUMNS[self.config.ascii.quality]

        # The "art" region gets 1 part out of 3 (art:info = 1:2 in
        # `layout["body"].split_row`) — NOT half the console width.
        # Using half-width here (the old bug) let the renderer produce
        # art wider than its actual panel, which either got clipped or
        # wrapped by the terminal. Height was never constrained at all,
        # so tall covers could also overflow the panel's bottom edge.
        console_w, console_h = self.console.size.width, self.console.size.height
        art_panel_w = max(1, console_w // 3)
        art_panel_h = max(1, console_h - (self.HEADER_H + self._visualizer_height() + self.CONTROLS_H))

        # Panel chrome: border (1 each side) + padding=(0, 1) (1 each
        # side horizontally, 0 vertically) from render_ascii_art.
        max_cols = max(10, art_panel_w - 4)
        max_rows = max(5, art_panel_h - 2)

        try:
            with Image.open(cover_path) as img:
                aspect = img.height / img.width if img.width else 1.0
        except (OSError, ZeroDivisionError, ValueError):
            aspect = 1.0

        # Letterbox-fit: given the renderer derives rows from
        # `cols * aspect / CHAR_ASPECT_COMPENSATION` (see
        # ascii_renderer._target_rows), solve the inverse for the column
        # count that would exactly fill the available rows, then take
        # whichever of the width- or height-based limit is smaller —
        # same idea as fitting a photo into a frame without cropping.
        cols_for_height = max_rows * CHAR_ASPECT_COMPENSATION / max(aspect, 1e-6)
        fitted = min(max_cols, cols_for_height)

        return max(10, min(base, int(fitted)))
