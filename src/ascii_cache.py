"""
ascii_cache.py
==============
ASCII conversion is expensive (multi-stage image pipeline over
thousands of pixels). This module ensures it only runs once per
unique (cover image, terminal size, render mode, quality) combination,
persisting rendered frames to disk so re-opening the same track is
instant.

Cache key = hash(cover file fingerprint, columns, mode, quality, color_mode).
Invalidated automatically whenever any of those inputs change.
"""

from __future__ import annotations

import hashlib
import logging
import pickle
from pathlib import Path

import session_cleanup
from ascii_renderer import AsciiFrame, AsciiRenderer
from config import AsciiConfig
from constants import CACHE_DIR
from utils import file_hash

logger = logging.getLogger(__name__)


class AsciiCache:
    def __init__(self, renderer: AsciiRenderer, cache_dir: Path = CACHE_DIR) -> None:
        self._renderer = renderer
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory: dict[str, AsciiFrame] = {}

    def _key(self, image_path: str, columns: int) -> str:
        cfg = self._renderer.config
        fingerprint = "|".join(
            [
                file_hash(image_path),
                str(columns),
                cfg.mode.value,
                cfg.quality.value,
                cfg.color_mode.value,
            ]
        )
        return hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()

    def get(self, image_path: str, columns: int) -> AsciiFrame:
        key = self._key(image_path, columns)

        if key in self._memory:
            return self._memory[key]

        disk_path = self._cache_dir / f"{key}.ascii"
        if disk_path.exists():
            try:
                frame: AsciiFrame = pickle.loads(disk_path.read_bytes())
                self._memory[key] = frame
                return frame
            except Exception as exc:  # noqa: BLE001
                logger.debug("Corrupt ASCII cache entry %s (%s); re-rendering", key, exc)

        frame = self._renderer.render(image_path, target_columns=columns)
        self._memory[key] = frame
        try:
            disk_path.write_bytes(pickle.dumps(frame))
            # online_source.py names covers it downloads "online-<hash>.jpg"
            # (see `_cache_cover`) — anything rendered from one of those is
            # itself an online-mode artifact, not a locally-scanned cover,
            # so it's a session-cleanup candidate too.
            if Path(image_path).name.startswith("online-"):
                session_cleanup.track(disk_path)
        except OSError as exc:
            logger.warning("Could not write ASCII cache to disk: %s", exc)
        return frame

    def invalidate_all(self) -> None:
        self._memory.clear()
        for f in self._cache_dir.glob("*.ascii"):
            f.unlink(missing_ok=True)

    def update_config(self, config: AsciiConfig) -> None:
        """Swap the renderer's config (e.g. quality/mode changed in Settings).
        Does not clear cache — the key already encodes config, so old
        entries simply stop being addressed.
        """
        self._renderer.config = config
