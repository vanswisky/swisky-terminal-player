"""
player.py
=========
High-level playback controller. Coordinates `AudioEngine` (mpv),
`QueueManager` (what plays next), and `LyricsManager` (sync), and
exposes the single API the UI/keyboard/command-palette layers call:
play, pause, next, previous, seek, volume, repeat, shuffle.

Also fires a `on_track_changed` callback so `ui.py` knows to trigger
an ASCII re-render and lyrics reload without polling.

`poll()` must be called once per frame from the app's main thread —
that's how a naturally-finished track turns into "advance to the next
one". See the comment on `AudioEngine._eof_pending` for why this is
polled rather than pushed straight from mpv's own event thread.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import online_source
from audio_engine import AudioEngine, EngineState
from config import PlaybackConfig, RepeatMode
from lyrics_manager import LyricsManager
from metadata import TrackMetadata
from queue_manager import QueueManager
from radio import RadioMixer

logger = logging.getLogger(__name__)

TrackChangedCallback = Callable[[Optional[TrackMetadata]], None]
LibraryProvider = Callable[[], list[TrackMetadata]]
OnlineRadioEnabledProvider = Callable[[], bool]

# How many online candidates to pull per topup search — a small
# multiple of what's actually needed, since some results fail to
# resolve (region-locked, taken down, no matching video) and get
# skipped rather than retried.
_ONLINE_RADIO_SEARCH_MULTIPLIER = 3
_ONLINE_RADIO_MIN_SEARCH = 6
# Resolve() is one yt-dlp extraction per candidate — the slow part.
# Running them in parallel (same pattern as online_source.py's own
# resolve_playlist_bulk) instead of one-at-a-time in the worker
# thread is most of the actual latency win.
_ONLINE_RADIO_RESOLVE_WORKERS = 4
# Cooldown after an online topup comes back with nothing (no network,
# no results for the seed) before trying again — see `_radio_online_retry_after`.
_ONLINE_RADIO_RETRY_COOLDOWN = 4.0

# Top up the queue once this few tracks remain ahead of the current
# one (see `QueueManager.remaining_ahead`). >0 so there's always at
# least one track already lined up while the new batch is fetched —
# fetching is in-process/cheap here (no network), but this keeps the
# gap real regardless.
RADIO_TOPUP_THRESHOLD = 2

# How many tracks played ago still count toward "recent taste" — kept
# small and separate from radio.py's own HISTORY_WINDOW so player.py
# doesn't need to know radio.py's internal tuning to size its buffer;
# it just needs to keep "at least enough".
MAX_HISTORY = 50


class Player:
    def __init__(self, config: PlaybackConfig, lyrics_manager: LyricsManager) -> None:
        self.config = config
        self.engine = AudioEngine(initial_volume=config.volume)
        self.queue = QueueManager()
        self.lyrics = lyrics_manager

        self._lock = threading.RLock()
        self._on_track_changed: list[TrackChangedCallback] = []
        self.muted = False
        self._pre_mute_volume = config.volume

        self._radio_mixer = RadioMixer()
        self._library_provider: LibraryProvider | None = None
        # Oldest-first list of tracks actually played this session —
        # feeds radio.py's artist/genre weighting. Deliberately not the
        # queue itself: the queue is "what's coming up", this is "what
        # actually played", which is what taste-matching should follow.
        self._history: list[TrackMetadata] = []

        # -- online radio fallback (Spotify-Radio/YT-Mix style) --------
        # When the local library can't fill a radio top-up (small/thin
        # library, or the taste-matched pool is simply exhausted),
        # `_maybe_extend_radio` falls back to searching+resolving fresh
        # tracks from `online_source.py` instead of just letting the
        # queue run dry. Network calls happen on a background thread
        # (same pattern as ui.py's online search) — `poll()` drains the
        # result onto the queue, so this never blocks the render loop.
        self._online_radio_enabled_provider: OnlineRadioEnabledProvider | None = None
        self._radio_online_lock = threading.Lock()
        self._radio_online_inflight = False
        self._radio_online_pending: list[TrackMetadata] | None = None
        # True from the moment `next()` finds nothing left to advance
        # to (queue genuinely empty right now) while radio is on, until
        # an online-resolved batch actually lands. This is what closes
        # the gap that used to just sit paused forever: before this,
        # `_drain_online_radio` only ever appended the new tracks to
        # the queue — nothing ever told playback to pick back up, so a
        # topup that finished a beat too late for `advance()` meant
        # silence until the user manually pressed next. Now the drain
        # itself resumes playback the instant it has something to play.
        self._queue_starved = False
        # Cheap backoff so a run of failed/empty online topups (no
        # network, no results for the seed) doesn't retry on literally
        # every single frame while starved — `_maybe_extend_radio`
        # fires every poll() tick, and each retry is a real network
        # search otherwise.
        self._radio_online_retry_after = 0.0

    # -- events -----------------------------------------------------------

    def on_track_changed(self, callback: TrackChangedCallback) -> None:
        self._on_track_changed.append(callback)

    def set_library_provider(self, provider: LibraryProvider) -> None:
        """Wire up where radio top-ups pull candidate tracks from —
        normally `PlaylistManager.library`. Optional: with none set,
        radio mode is simply a no-op (checked in `_maybe_extend_radio`)
        rather than an error, so tests/tools that construct a `Player`
        standalone don't need to fake a whole library.
        """
        self._library_provider = provider

    def set_online_radio_enabled_provider(self, provider: OnlineRadioEnabledProvider) -> None:
        """Wire up a live "is online mode enabled" check (normally
        `lambda: app_config.online.enabled`) so the online radio
        fallback respects the user's Settings toggle without `Player`
        needing to hold the whole `AppConfig`. Optional: with none set,
        the online fallback simply never fires (checked in
        `_maybe_start_online_radio_topup`), same "off by default,
        no-op rather than error" pattern as `_library_provider`.
        """
        self._online_radio_enabled_provider = provider

    def _notify_track_changed(self) -> None:
        track = self.queue.current()
        for cb in list(self._on_track_changed):
            try:
                cb(track)
            except Exception:  # noqa: BLE001
                logger.exception("track_changed callback raised")

    # -- queue / loading ----------------------------------------------------

    def load_queue(self, tracks: list[TrackMetadata], start_index: int = 0) -> None:
        with self._lock:
            self.queue.load(tracks, start_index)
            self._play_current()

    def play_index(self, index: int) -> None:
        """Jump straight to a specific position in the queue (e.g. the
        user pressing Enter on a row in the Queue screen). Public
        counterpart to the old direct `queue.jump_to()` + private
        `_play_current()` call `ui.py` used to make.
        """
        with self._lock:
            if self.queue.jump_to(index) is not None:
                self._play_current()

    def play_online_next(self, track: TrackMetadata) -> None:
        """Insert an online-resolved track right after whatever's
        currently playing and jump to it immediately — used by the
        Online Search screen's Enter key. Doesn't disturb the rest of
        the queue.
        """
        with self._lock:
            self.queue.add_next(track)
            self.play_index(self.queue.cursor + 1)

    def _load_track(self, track: TrackMetadata) -> None:
        """Shared by `_play_current`/`next`/`previous` so online tracks'
        `stream_headers` (needed by some CDNs to avoid a 403) get
        forwarded to mpv the same way regardless of which of those
        three called us.
        """
        self.engine.load(track.path, http_headers=track.stream_headers)
        self.engine.set_speed(self.config.speed)
        self.lyrics.load_for_track(track)
        self._record_history(track)
        self._notify_track_changed()

    def _record_history(self, track: TrackMetadata) -> None:
        self._history.append(track)
        if len(self._history) > MAX_HISTORY:
            del self._history[: len(self._history) - MAX_HISTORY]

    def _maybe_extend_radio(self) -> None:
        """Top up the queue from the library when radio mode is on and
        few tracks remain ahead. Called right before `advance()` so
        the fresh tracks are already in the queue by the time it looks
        for "what's next" — cheap to call unconditionally since it
        exits immediately when radio is off or the queue isn't
        actually low yet.

        Local library first, same as before. If that comes up short —
        no `_library_provider` wired, an empty/too-small library, or
        `RadioMixer` simply running out of untried local candidates —
        the shortfall is handed to the online fallback below instead
        of silently topping up with fewer tracks than intended.
        """
        if not self.config.radio:
            return
        if self.config.repeat == RepeatMode.ONE:
            return  # advance() never leaves the current track anyway
        if self.queue.remaining_ahead(self.config.shuffle) > RADIO_TOPUP_THRESHOLD:
            return

        batch: list[TrackMetadata] = []
        if self._library_provider is not None:
            try:
                library = self._library_provider()
            except Exception:  # noqa: BLE001 — a broken provider shouldn't kill playback
                logger.exception("library_provider raised during radio top-up")
                library = []
            if library:
                batch = self._radio_mixer.next_batch(
                    library=library,
                    queue_paths=self.queue.paths(),
                    history=self._history,
                )
                for track in batch:
                    self.queue.add_end(track)

        shortfall = self._radio_mixer.batch_size - len(batch)
        if shortfall > 0:
            self._maybe_start_online_radio_topup(shortfall)

    # -- online radio fallback --------------------------------------------

    def _maybe_start_online_radio_topup(self, need: int) -> None:
        """Kicks off a background search+resolve for `need` fresh
        tracks when the local library couldn't supply enough (or any)
        radio candidates. No-op when online mode is unavailable/off,
        or a previous topup is still in flight — `_maybe_extend_radio`
        gets called on basically every `next()`, so without the
        in-flight guard a queue that stays low for a few tracks in a
        row would fire off a new search thread every time instead of
        waiting for the one already running.
        """
        provider = self._online_radio_enabled_provider
        if provider is None:
            return
        try:
            if not provider():
                return
        except Exception:  # noqa: BLE001 — a broken provider shouldn't kill playback
            logger.exception("online_radio_enabled_provider raised")
            return

        with self._radio_online_lock:
            if self._radio_online_inflight:
                return
            if time.monotonic() < self._radio_online_retry_after:
                # A recent attempt came back empty (no network, no
                # results for the seed) — back off instead of firing a
                # fresh search on literally every poll() tick while
                # starved.
                return
            self._radio_online_inflight = True

        seed = self._build_online_radio_seed()
        exclude = self._online_radio_exclude_titles()
        threading.Thread(
            target=self._online_radio_worker,
            args=(seed, need, exclude),
            daemon=True,
        ).start()

    def _build_online_radio_seed(self) -> str:
        """Picks a search seed the same way YT Music/Spotify Radio
        "seed" off what's actually been playing: most recent artist
        first (most specific taste signal), falling back to the most
        recent genre, then a generic mix so a brand-new session with
        no history yet still gets *something* instead of the topup
        silently doing nothing.
        """
        for track in reversed(self._history):
            if track.artist and track.artist != "Unknown Artist":
                return track.artist
        for track in reversed(self._history):
            if track.genre and track.genre != "Unknown":
                return f"{track.genre} music mix"
        return "popular music mix"

    def _online_radio_exclude_titles(self) -> set[str]:
        """Lightweight de-dupe key (lowercased "artist - title") built
        from history + current queue, so a topup doesn't hand back a
        track that's already just played or already queued up.
        """
        seen: set[str] = set()
        for track in list(self._history) + list(self.queue.items):
            seen.add(f"{track.artist} - {track.title}".strip().lower())
        return seen

    def _online_radio_worker(self, seed: str, need: int, exclude: set[str]) -> None:
        resolved: list[TrackMetadata] = []
        try:
            limit = max(_ONLINE_RADIO_MIN_SEARCH, need * _ONLINE_RADIO_SEARCH_MULTIPLIER)
            results = online_source.search(seed, limit=limit)
            random.shuffle(results)  # don't always take iTunes/YouTube's top hits verbatim
            candidates = [
                r for r in results
                if f"{r.artist} - {r.title}".strip().lower() not in exclude
            ]
            pool = ThreadPoolExecutor(
                max_workers=min(_ONLINE_RADIO_RESOLVE_WORKERS, max(1, len(candidates)))
            )
            try:
                futures = [pool.submit(online_source.resolve, r) for r in candidates]
                for future in as_completed(futures):
                    if len(resolved) >= need:
                        break
                    try:
                        resolved.append(future.result())
                    except online_source.OnlineSourceError as exc:
                        logger.debug("Radio online-resolve skipped: %s", exc)
                        continue
            finally:
                # Don't block this worker waiting for stragglers once
                # `need` is met — let them finish in the background (or
                # get dropped) instead of holding up the queue topup.
                pool.shutdown(wait=False, cancel_futures=True)
        except online_source.OnlineSourceError as exc:
            logger.debug("Radio online-search failed for seed %r: %s", seed, exc)
        except Exception:  # noqa: BLE001 — a topup failure must never take playback down
            logger.exception("Unexpected error during online radio top-up")

        with self._radio_online_lock:
            self._radio_online_pending = resolved
            self._radio_online_inflight = False
            if not resolved:
                self._radio_online_retry_after = time.monotonic() + _ONLINE_RADIO_RETRY_COOLDOWN

    def _drain_online_radio(self) -> None:
        """Moves whatever the background worker resolved onto the
        actual queue. Called from `poll()` (main thread, every frame)
        — never touch the queue from `_online_radio_worker`'s thread
        directly, same reasoning as every other mpv/queue command in
        this class.

        If the queue had genuinely run dry waiting for this batch
        (`_queue_starved`), also picks playback back up immediately —
        without this, a topup that landed a moment too late for
        `advance()` in `next()` just sat in the queue forever with
        nothing ever telling mpv to load it, which is what made radio
        mode look like it "paused" instead of continuing (Spotify/YT
        Music never leave a real gap like that: they keep the mix
        going the instant the next track is ready).
        """
        with self._radio_online_lock:
            pending = self._radio_online_pending
            self._radio_online_pending = None
        if not pending:
            return
        # Radio may have been turned off (or the queue refilled from
        # local tracks) while this batch was resolving on the network
        # — don't splice stale online tracks into an unrelated queue.
        if not self.config.radio:
            return
        with self._lock:
            for track in pending:
                self.queue.add_end(track)
            if self._queue_starved:
                self._queue_starved = False
                next_track = self.queue.advance(self.config.repeat, self.config.shuffle)
                if next_track is not None:
                    self._load_track(next_track)

    def _play_current(self) -> None:
        track = self.queue.current()
        if track is None:
            self.engine.stop()
            self._notify_track_changed()
            return
        self._load_track(track)

    # -- transport ------------------------------------------------------

    def toggle_play_pause(self) -> None:
        self.engine.toggle_pause()

    def pause(self) -> None:
        self.engine.pause()

    def resume(self) -> None:
        self.engine.play()

    def stop(self) -> None:
        self.engine.stop()

    def next(self) -> None:
        with self._lock:
            self._maybe_extend_radio()
            track = self.queue.advance(self.config.repeat, self.config.shuffle)
            if track is None:
                # With radio on, this doesn't necessarily mean "truly
                # nothing left" — the online topup just triggered above
                # may still be resolving on its background thread.
                # Flag it so `_drain_online_radio` knows to resume
                # playback itself the moment that batch lands, instead
                # of the queue staying silent until the user manually
                # hits next again.
                self._queue_starved = self.config.radio
                self.engine.stop()
                self._notify_track_changed()
                return
            self._queue_starved = False
            self._load_track(track)

    def previous(self) -> None:
        with self._lock:
            # If more than 3s into the track, restart it instead of
            # jumping back a track (standard music-player UX).
            if self.engine.snapshot().position > 3.0:
                self.engine.seek_to(0)
                return
            track = self.queue.previous(self.config.shuffle)
            if track is None:
                return
            self._load_track(track)

    def poll(self) -> None:
        """Call once per frame from the main thread. Detects a track
        that finished playing naturally and advances the queue.

        Also runs the radio topup check every frame (not just inside
        `next()`) — this is what actually makes online radio topups
        fast in practice: `_maybe_extend_radio` starts the
        search+resolve as soon as the queue gets thin, while the
        current track is still playing, instead of only firing once
        the track has already ended. `_drain_online_radio` then folds
        the result in the moment it's ready. Both are cheap no-ops
        when radio's off or the queue isn't low, so calling them
        unconditionally here is fine.

        Deliberately not driven by mpv's own end-file callback — that
        callback runs on mpv's internal event thread, and issuing
        another blocking mpv command (loading the next track) from
        inside it can deadlock libmpv's single-threaded event loop.
        Polling keeps every mpv command on one, safe, predictable
        thread. This is also what used to make the player "only play
        one song": the auto-advance call from that thread would hang
        rather than actually loading the next track.
        """
        self._drain_online_radio()
        self._maybe_extend_radio()
        if self.engine.consume_eof():
            self.next()

    # -- seeking -----------------------------------------------------

    def seek_relative(self, seconds: float) -> None:
        self.engine.seek(seconds, relative=True)

    def seek_absolute(self, seconds: float) -> None:
        self.engine.seek_to(seconds)

    def seek_to_fraction(self, fraction: float) -> None:
        """Used for mouse-click seeking on the progress bar (fraction 0..1)."""
        state = self.engine.snapshot()
        if state.duration > 0:
            self.engine.seek_to(max(0.0, min(1.0, fraction)) * state.duration)

    # -- volume ----------------------------------------------------------

    def volume_up(self, step: int | None = None) -> None:
        step = step or self.config.volume_step
        self.set_volume(self.engine.snapshot().volume + step)

    def volume_down(self, step: int | None = None) -> None:
        step = step or self.config.volume_step
        self.set_volume(self.engine.snapshot().volume - step)

    def set_volume(self, volume: int) -> None:
        volume = max(0, min(150, volume))
        self.engine.set_volume(volume)
        self.config.volume = volume

    def toggle_mute(self) -> None:
        self.muted = not self.muted
        self.engine.set_mute(self.muted)

    # -- modes -----------------------------------------------------------

    def cycle_repeat(self) -> RepeatMode:
        order = [RepeatMode.OFF, RepeatMode.ALL, RepeatMode.ONE]
        idx = (order.index(self.config.repeat) + 1) % len(order)
        self.config.repeat = order[idx]
        return self.config.repeat

    def toggle_shuffle(self) -> bool:
        self.config.shuffle = not self.config.shuffle
        return self.config.shuffle

    def toggle_radio(self) -> bool:
        self.config.radio = not self.config.radio
        if self.config.radio:
            # Kick the first topup off immediately instead of waiting
            # for the next poll() tick — irrelevant for a long queue,
            # but when radio's turned on with the queue already thin
            # (small local library) this is what gives the online
            # fallback the most possible lead time before the current
            # track ends.
            with self._lock:
                self._maybe_extend_radio()
        return self.config.radio

    def set_speed(self, speed: float) -> None:
        self.config.speed = speed
        self.engine.set_speed(speed)

    # -- state -------------------------------------------------------

    def snapshot(self) -> EngineState:
        return self.engine.snapshot()

    def current_track(self) -> TrackMetadata | None:
        return self.queue.current()

    def shutdown(self) -> None:
        self.engine.shutdown()
