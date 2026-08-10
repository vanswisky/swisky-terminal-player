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

def render_ascii_art(
    frame: AsciiFrame | None, theme: Theme, width: int | None = None, height: int | None = None
) -> Panel:
    """`width`/`height`, when given, are the *outer* panel dimensions
    (border included) to force — a `rich.panel.Panel` sizes itself to
    its own content by default and does NOT stretch to fill whatever
    `Layout` region it's placed in, so without this the panel would
    only wrap tightly around the ASCII art. `ui.py::_art_geometry`
    derives both from the cover's own aspect ratio (not from however
    much room the surrounding layout happens to have that frame), so
    the box stays a consistent shape and `ui.py` centers it — with
    `Align` — in whatever space is left rather than stretching it.

    Centering (rather than stretching) the art inside that forced
    size is what keeps the cover itself undistorted — only the
    surrounding border would grow/shrink, never the image.
    """
    if frame is None:
        inner = Text("No cover art", style=theme.text_muted)
    else:
        inner = Text.from_ansi(frame.ansi_text)

    if height is not None:
        # Panel chrome here is a 1-char border on top/bottom (padding
        # is horizontal-only: `padding=(0, 1)`), so the interior has
        # exactly `height - 2` rows to center within.
        inner_height = max(1, height - 2)
        body = Align(inner, align="center", vertical="middle", height=inner_height)
    else:
        body = Align.center(inner, vertical="middle")

    return Panel(
        body,
        border_style=theme.border,
        padding=(0, 1),
        width=width,
        height=height,
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
        status = "Searching online for lyrics..." if state.fetching else "No synced lyrics found"
        return Group(Text("LYRICS", style=f"bold {theme.text_muted}"), Text(status, style=theme.text_muted))

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

def render_visualizer(
    frame: SpectrumFrame | None, theme: Theme, width: int, height: int = 8,
    bordered: bool = True,
) -> Panel | Group:
    """cava-style: skinny 1-column bars with a 1-column gap between
    each one (cava's default look — distinct bars, not a solid packed
    block), instead of the old edge-to-edge fill. Each bar still uses
    `VISUALIZER_BARS_RAMP`'s eighths-block glyphs for its topmost,
    partially-filled row, same sub-cell precision as before — only the
    spacing changed.

    `bordered=True` (the original full-width placement, kept for any
    future/legacy caller) wraps the bars in a titled Panel. `ui.py` now
    places this compactly under the cover art instead, where a border
    and title would eat rows that matter more as bar height on a small
    strip — `bordered=False` returns just the bar rows as a bare Group,
    letting the caller control padding/alignment itself.
    """
    if frame is None or width <= 0:
        body = Text("")
    else:
        bands = frame.bands
        # One gap column between bars eats into how many actually fit —
        # `bar_count` bars plus `bar_count - 1` single-space gaps must
        # still fit in `width`.
        bar_count = max(1, (width + 1) // 2)
        n = len(bands)
        # Resample band count to how many bars actually fit so the
        # visualizer always spans the available width regardless of the
        # band_count setting.
        if n != bar_count and n > 0:
            xs = [int(i * n / bar_count) for i in range(bar_count)]
            values = [bands[i] for i in xs]
        else:
            values = list(bands[:bar_count])

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
            rows.append(Text(" ".join(chars), style=theme.accent))
        body = Group(*rows)

    if not bordered:
        return body
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
    """8 segments matching the spec layout:
    VOL | PREV | PLAY | NEXT | REPEAT | SHUFFLE | QUEUE | EXIT

    SETTINGS moved out of this bar and into the header (top-left,
    next to the search bar — see `ui.py::_render_header`); still
    reachable the same three ways it always was (click, `Esc`, or the
    `settings` idea generally) — this row just doesn't carry the
    button anymore. PLAYLISTS (saved playlists — distinct from QUEUE,
    the *current* play queue) lives in the header too, as a brand new
    button next to it — there was no way at all to reach the saved
    playlist browser before.

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

    vol_icon = "󰝟" if muted else "၊၊||၊ "
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
        cell(f"", active=shuffle),
        cell(""),
        cell("➜]"),
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


def render_playlists(
    names: list[str],
    selected: int,
    counts: dict[str, int],
    theme: Theme,
) -> Table:
    """The saved-playlist browser (`ScreenMode.PLAYLISTS` — distinct
    from `render_queue` above, which shows the *current* play queue).
    One row per playlist saved via `playlist_manager.save_playlist`
    (manually, or automatically when importing an online playlist —
    see `ui.py::_finish_playlist_import`), with track count so an
    empty/broken save is obvious before loading it.

    `counts` maps playlist name -> track count, precomputed by
    `ui.py` (it already has the `PlaylistManager` and doesn't need
    this widget re-touching disk on every frame).
    """
    table = Table(expand=True, border_style=theme.border, header_style=f"bold {theme.text_muted}")
    table.add_column("#", width=4)
    table.add_column("Playlist")
    table.add_column("Tracks", width=8, justify="right")

    if not names:
        table.add_row(Text(""), Text("No saved playlists yet.", style=theme.text_muted), Text(""))
        return table

    for i, name in enumerate(names):
        is_selected = i == selected
        style = f"bold {theme.accent}" if is_selected else theme.text_secondary
        marker = "▶" if is_selected else str(i + 1)
        row_style = f"on {theme.surface}" if is_selected else None
        table.add_row(
            Text(marker, style=style),
            Text(name, style=style, overflow="ellipsis"),
            Text(str(counts.get(name, 0)), style=style),
            style=row_style,
        )
    return table


def render_search(
    query: str, results, cursor: int, status: str, theme: Theme,
    show_query: bool = True, mode: str = "track",
) -> Group:
    """Online search results screen: an optional typed-query line, an
    optional status/spinner line (searching / resolving / error), and
    a results table once a search has returned something.

    `results` holds either `online_source.OnlineSearchResult` objects
    (mode="track" — duck-typed on `.title`/`.artist`/`.duration`) or
    `online_source.OnlinePlaylistResult` objects (mode="playlist" —
    duck-typed on `.title`/`.owner`/`.track_count`) — only ever one
    kind at a time, matching whichever search was last run. This
    widget doesn't import `online_source` just to draw either shape.

    `show_query=False` skips the query line entirely — used when the
    header itself is doing double duty as the live input box (see
    `ui.py::_build_search_layout`), so the query isn't shown twice.
    """
    parts: list = []
    if show_query:
        parts.append(Text(f"🔍 {query}_", style=f"bold {theme.accent}"))
    if status:
        parts.append(Text(status, style=theme.text_muted))

    if results:
        table = Table(expand=True, border_style=theme.border, header_style=f"bold {theme.text_muted}")
        table.add_column("#", width=4)
        if mode == "playlist":
            table.add_column("Playlist")
            table.add_column("Owner")
            table.add_column("Tracks", width=7, justify="right")
        else:
            table.add_column("Title")
            table.add_column("Channel")
            table.add_column("Dur", width=6, justify="right")
        for i, result in enumerate(results):
            selected = i == cursor
            style = f"bold {theme.accent}" if selected else theme.text_secondary
            marker = "▶" if selected else str(i + 1)
            row_style = f"on {theme.surface}" if selected else None
            if mode == "playlist":
                count = result.track_count
                count_text = str(count) if count else "?"
                table.add_row(
                    Text(marker, style=style),
                    Text(result.title, style=style, overflow="ellipsis"),
                    Text(result.owner, style=style, overflow="ellipsis"),
                    Text(count_text, style=style),
                    style=row_style,
                )
            else:
                table.add_row(
                    Text(marker, style=style),
                    Text(result.title, style=style, overflow="ellipsis"),
                    Text(result.artist, style=style, overflow="ellipsis"),
                    Text(format_time(result.duration), style=style),
                    style=row_style,
                )
        parts.append(Text(""))
        parts.append(table)
    elif not status:
        hint = "Type a query and press Enter to search." if mode == "track" \
            else "Type a query and press Enter to search playlists."
        parts.append(Text(hint, style=theme.text_muted))

    return Group(*parts)


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
