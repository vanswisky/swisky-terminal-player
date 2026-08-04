"""
widgets.py
==========
Pure render functions: (state) -> `rich` renderable. Nothing in this
module mutates application state or touches I/O — it only knows how
to draw. Keeping this separate from `ui.py` (which owns the Live loop
and layout) makes each widget easy to reason about and test in
isolation.
"""

from __future__ import annotations

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from ascii_renderer import AsciiFrame
from config import RepeatMode
from constants import VISUALIZER_BARS_RAMP
from lyrics_manager import LyricsState
from metadata import TrackMetadata
from theme import Theme
from utils import format_time
from visualizer import SpectrumFrame


# --------------------------------------------------------------------------
# ASCII album art
# --------------------------------------------------------------------------

def render_ascii_art(frame: AsciiFrame | None, theme: Theme) -> Panel:
    if frame is None:
        body = Align.center(
            Text("No cover art", style=theme.text_muted), vertical="middle"
        )
    else:
        body = Text.from_ansi(frame.ansi_text)
    return Panel(
        body,
        border_style=theme.border,
        padding=(0, 1),
    )


# --------------------------------------------------------------------------
# Now playing / metadata panel
# --------------------------------------------------------------------------

def render_now_playing(
    track: TrackMetadata | None,
    theme: Theme,
) -> Group:
    """Title / artist / album metadata only. The progress bar is rendered
    separately by `render_progress_bar` in its own fixed-size layout region
    so `ui.py` can compute its exact screen row for mouse-click seeking —
    embedding it inside a variable-height text block would make that
    impossible to do reliably.
    """
    if track is None:
        return Group(Text("NOW PLAYING", style=f"bold {theme.text_muted}"), Text("No track loaded", style=theme.text_muted))

    header = Text("NOW PLAYING", style=f"bold {theme.text_muted}")
    title = Text(track.title, style=f"bold {theme.text_primary}", overflow="ellipsis")
    artist = Text(track.artist, style=theme.accent)

    info = Table.grid(padding=(0, 1))
    info.add_column(style=theme.text_muted)
    info.add_column(style=theme.text_secondary)
    info.add_row("Album", track.album)

    return Group(header, title, artist, Text(""), info)


def render_progress_bar(position: float, duration: float, theme: Theme) -> Table:
    grid = Table.grid(expand=True)
    grid.add_column(width=6, justify="right")
    grid.add_column(ratio=1)
    grid.add_column(width=6, justify="left")

    bar = ProgressBar(
        total=max(duration, 0.001),
        completed=min(position, duration) if duration else 0,
        complete_style=theme.accent,
        finished_style=theme.accent,
        style=theme.border,
    )
    grid.add_row(
        Text(format_time(position), style=theme.text_secondary),
        bar,
        Text(format_time(duration), style=theme.text_muted),
    )
    return grid


# --------------------------------------------------------------------------
# Lyrics
# --------------------------------------------------------------------------

def render_lyrics(state: LyricsState, position: float, theme: Theme, context: int = 3) -> Group:
    if not state.available:
        return Group(Text("LYRICS", style=f"bold {theme.text_muted}"), Text("No synced lyrics found", style=theme.text_muted))

    active = state.active_index(position)
    lines = []
    total = len(state.lines)
    start = max(0, active - context)
    end = min(total, active + context + 1)

    for i in range(start, end):
        line = state.lines[i]
        if i == active:
            lines.append(Text(f"▶ {line.text}", style=f"bold {theme.accent}"))
        elif i < active:
            lines.append(Text(f"  {line.text}", style=theme.text_muted))
        else:
            lines.append(Text(f"  {line.text}", style=theme.text_secondary))

    return Group(Text("LYRICS", style=f"bold {theme.text_muted}"), *lines)


# --------------------------------------------------------------------------
# Spectrum visualizer (full width bar)
# --------------------------------------------------------------------------

def render_visualizer(frame: SpectrumFrame | None, theme: Theme, width: int, height: int = 8) -> Panel:
    if frame is None or width <= 0:
        body = Text("")
    else:
        bands = frame.bands
        n = len(bands)
        # Resample band count to available width so the visualizer always
        # spans the full terminal width regardless of band_count setting.
        if n != width and n > 0:
            xs = [int(i * n / width) for i in range(width)]
            values = [bands[i] for i in xs]
        else:
            values = list(bands)

        ramp = VISUALIZER_BARS_RAMP
        rows = []
        for row_i in range(height, 0, -1):
            threshold = row_i / height
            chars = []
            for v in values:
                if v >= threshold:
                    chars.append("█")
                elif v >= threshold - (1 / height):
                    idx = min(len(ramp) - 1, int((v - (threshold - 1 / height)) * height * (len(ramp) - 1)))
                    chars.append(ramp[max(1, idx)])
                else:
                    chars.append(" ")
            rows.append(Text("".join(chars), style=theme.accent))
        body = Group(*rows)

    return Panel(body, title="REALTIME AUDIO SPECTRUM", title_align="left",
                 border_style=theme.border, style=theme.background)


# --------------------------------------------------------------------------
# Control bar
# --------------------------------------------------------------------------

def render_control_bar(
    theme: Theme,
    volume: int,
    paused: bool,
    repeat: RepeatMode,
    shuffle: bool,
    muted: bool,
    column_widths: list[int],
) -> Table:
    """9 segments matching the spec layout:
    VOL | PREV | PLAY | NEXT | REPEAT | SHUFFLE | QUEUE | SETTINGS | EXIT

    `column_widths` gives each column's exact rendered width in
    characters, computed once by `ui.py::_control_bar_geometry` and
    reused for both this draw call and mouse hit-testing — so a click
    and the button under it can't silently drift apart the way they
    used to when this table's columns were auto-sized from content
    while the click math assumed equal division.

    Column order here MUST stay in sync with `ui.py::CONTROL_BAR_SEGMENTS`.
    """
    grid = Table.grid(expand=False, padding=0)
    for w in column_widths:
        grid.add_column(justify="center", width=w, no_wrap=True, overflow="crop")

    vol_icon = "🔇" if muted else "၊၊||၊ "
    play_icon = "▶" if paused else "⏸"
    repeat_label = {RepeatMode.OFF: "🗘", RepeatMode.ONE: "🗘 1x", RepeatMode.ALL: "🗘"}[repeat]

    def cell(label: str, active: bool = False) -> Text:
        return Text(label, style=f"bold {theme.accent}" if active else theme.text_secondary, justify="center")

    grid.add_row(
        cell(f"{vol_icon}{volume}%"),
        cell("⏮"),
        cell(play_icon),
        cell("⏭"),
        cell(repeat_label, active=repeat != RepeatMode.OFF),
        cell(f"⇆", active=shuffle),
        cell("QUEUE ☰"),
        cell("SETTINGS ⚙"),
        cell("EXIT ✕"),
    )
    return grid


# --------------------------------------------------------------------------
# Queue / Playlist panel
# --------------------------------------------------------------------------

def render_queue(
    items,
    selected: int,
    playing_index: int,
    theme: Theme,
    max_rows: int = 12,
    scroll_offset: int = 0,
) -> Table:
    """Renders a scrolled window of `max_rows` items starting at
    `scroll_offset`.

    `selected` (the keyboard cursor `ui.py` moves with UP/DOWN) and
    `playing_index` (the track actually loaded in the engine) are
    tracked separately and shown differently — a highlighted row for
    the former, a "▶" marker for the latter. Previously this widget
    only ever took one `cursor` value, always the *playing* track, so
    moving the on-screen cursor with the arrow keys never changed
    anything visible: it looked exactly like the keys did nothing.

    `ui.py` is responsible for keeping `scroll_offset` in sync with
    `selected` so the highlighted row is always visible.
    """
    table = Table(expand=True, border_style=theme.border, header_style=f"bold {theme.text_muted}")
    table.add_column("#", width=4)
    table.add_column("Title")
    table.add_column("Artist")
    table.add_column("Dur", width=6, justify="right")

    window = items[scroll_offset:scroll_offset + max_rows]
    for row_i, track in enumerate(window):
        i = scroll_offset + row_i
        is_selected = i == selected
        is_playing = i == playing_index
        style = f"bold {theme.accent}" if is_selected else theme.text_secondary
        marker = "▶" if is_playing else str(i + 1)
        row_style = f"on {theme.surface}" if is_selected else None
        table.add_row(
            Text(marker, style=f"bold {theme.accent}" if is_playing else style),
            Text(track.title, style=style),
            Text(track.artist, style=style),
            Text(format_time(track.duration), style=style),
            style=row_style,
        )
    return table


# --------------------------------------------------------------------------
# Settings panel
# --------------------------------------------------------------------------

def render_settings(config, theme: Theme, cursor: int = -1) -> Table:
    """Draws one row per entry in `settings_items.SETTINGS_ITEMS`, with
    the row at `cursor` highlighted and flanked by `‹ ›` to signal it's
    the one LEFT/RIGHT will change. Sourcing the row list from
    `settings_items` (instead of hand-listing config fields here, as
    before) keeps this in lockstep with what `ui.py` can actually
    navigate to and edit.
    """
    from settings_items import SETTINGS_ITEMS

    table = Table.grid(padding=(0, 2))
    table.add_column(style=f"bold {theme.text_secondary}", width=18)
    table.add_column(style=theme.accent)

    for i, item in enumerate(SETTINGS_ITEMS):
        selected = i == cursor
        label_style = f"bold {theme.accent}" if selected else theme.text_secondary
        value = item.display(config)
        value_text = Text(f"‹ {value} ›" if selected else value, style=f"bold {theme.accent}" if selected else theme.accent)
        table.add_row(Text(item.label, style=label_style), value_text)
    return table
