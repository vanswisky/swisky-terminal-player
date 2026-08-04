"""
ui.py
=====
Owns the `rich.Live` render loop and terminal layout. This is the
integration point: it wires keyboard/mouse input to `player.py`,
asks `ascii_cache.py` for the current cover's ASCII frame, asks
`visualizer.py` for the current spectrum, and asks `widgets.py` to
draw all of it into a `Layout` matching the spec:

    ┌───────────────────────────────────────────┐
    │            CINEMATIC HEADER                │
    ├───────────────────┬─────────────────────────┤
    │                   │  NOW PLAYING / LYRICS    │
    │   ASCII ALBUM ART │  (enlarged, no dummy     │
    │                   │   "features" panel)      │
    ├───────────────────┴─────────────────────────┤
    │        REALTIME AUDIO SPECTRUM (full width)  │
    ├───────────────────────────────────────────────┤
    │  VOL│PREV│PLAY│NEXT│REPEAT│SHUFFLE│QUEUE│...      │
    └───────────────────────────────────────────────┘

Screen modes (mutually exclusive overlays): NORMAL, QUEUE, SETTINGS,
COMMAND_PALETTE. Only one owns keyboard focus at a time.
"""

from __future__ import annotations

import enum
import logging
import time

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

import widgets
from ascii_cache import AsciiCache
from command_palette import CommandPalette
from config import AppConfig, AsciiRenderMode, RepeatMode
from keyboard_handler import KeyboardHandler
from lyrics_manager import LyricsManager
from mouse_handler import MouseAction, disable_mouse_reporting, enable_mouse_reporting, parse_mouse_sequence
from playlist_manager import PlaylistManager, SortKey
from player import Player
from queue_manager import QueueManager
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
        self._last_art_size: tuple[int, int] | None = None
        self._ascii_frame = None

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

        # Control bar buttons.
        if self.mode == ScreenMode.NORMAL and self._control_row is not None and event.row == self._control_row:
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

    def _build_layout(self) -> Layout:
        theme = self.theme_mgr.current
        state = self.player.snapshot()
        track = self.player.current_track()

        current_size = (self.console.size.width, self.console.size.height)
        size_changed = current_size != self._last_art_size
        if track and track.cover_path and (track.cover_path != self._last_cover or size_changed):
            self._ascii_frame = self.ascii_cache.get(
                track.cover_path, columns=self._art_columns(track.cover_path)
            )
            self._last_cover = track.cover_path
            self._last_art_size = current_size
        elif track is None:
            self._ascii_frame = None

        layout = Layout()
        layout.split_column(
            Layout(name="header", size=self.HEADER_H),
            Layout(name="body", ratio=1),
            Layout(name="visualizer", size=self.VISUALIZER_H),
            Layout(name="controls", size=self.CONTROLS_H),
        )
        layout["body"].split_row(
            Layout(name="art", ratio=1),
            Layout(name="info", ratio=2),
        )

        layout["header"].update(
            Panel(Text(f"Swisky~", justify="center",
                        style=f"bold {theme.accent}"), border_style=theme.border)
        )
        layout["art"].update(widgets.render_ascii_art(self._ascii_frame, theme))

        now_playing = widgets.render_now_playing(track, theme)
        progress_bar = widgets.render_progress_bar(state.position, state.duration, theme)

        if self.config.lyrics.enabled and self.config.lyrics.auto_scroll:
            info_body = Group(
                now_playing,
                Text(""),
                progress_bar,
                Text(""),
                widgets.render_lyrics(self.lyrics.state(), state.position, theme),
            )
        else:
            info_body = Group(now_playing, Text(""), progress_bar)
        layout["info"].update(Panel(info_body, border_style=theme.border, padding=(1, 2)))

        if self.config.visualizer.enabled:
            spec_frame = self.spectrum.decay() if state.paused else self.spectrum.analyze_at(state.position)
        else:
            spec_frame = None
        vis_width = max(10, self.console.size.width - 4)
        layout["visualizer"].update(widgets.render_visualizer(spec_frame, theme, vis_width, height=8))

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
        # (header=3, visualizer=10, controls=3) plus the known internal
        # structure of the widgets those regions contain. Column split
        # (art:info = 1:2) is approximate since Rich's ratio-resolver
        # doesn't expose exact cell boundaries — close enough for a
        # terminal mouse click.
        HEADER_H, VISUALIZER_H, CONTROLS_H = self.HEADER_H, self.VISUALIZER_H, self.CONTROLS_H
        console_w, console_h = self.console.size.width, self.console.size.height

        info_panel_left = int(console_w * 1 / 3) + 1  # art:info ratio is 1:2
        info_content_left = info_panel_left + 1 + 2    # +1 border, +2 padding

        # Content line offsets inside the NOW PLAYING Group (see
        # widgets.render_now_playing): title, artist, blank, 1 info
        # row (just Album now — Genre/Codec were dropped so there's
        # more vertical room left for lyrics on short terminals),
        # blank, then the progress bar grid.
        PROGRESS_LINE_OFFSET = 7
        self._progress_row = HEADER_H + 1 + 1 + PROGRESS_LINE_OFFSET  # +1 border, +1 padding-top
        # The bar itself sits after a 6-char time label + 1 space gutter.
        bar_left = info_content_left + 7
        bar_right = console_w - 3 - 6  # panel right border/padding + trailing time label
        self._progress_cols = (bar_left, max(bar_left + 1, bar_right))

        # Controls panel occupies the last CONTROLS_H rows; row +1 is the
        # top border, row +2 is where the button text actually renders.
        self._control_row = console_h - CONTROLS_H + 2

        overlay = self._build_overlay(theme)
        if overlay is not None:
            return Layout(overlay)
        return layout

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
        art_panel_h = max(1, console_h - (self.HEADER_H + self.VISUALIZER_H + self.CONTROLS_H))

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
