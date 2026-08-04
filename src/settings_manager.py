"""
settings_manager.py
====================
Loads and persists `AppConfig` to `config.json`. All mutation of
settings at runtime (from the Settings panel or the Command Palette)
should go through `SettingsManager.update(...)` so changes are saved
atomically and observers are notified.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from config import AppConfig
from constants import CONFIG_PATH

logger = logging.getLogger(__name__)

Listener = Callable[[AppConfig], None]


class SettingsManager:
    """Owns the on-disk lifecycle of `AppConfig`."""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self._path = path
        self._config = self._load()
        self._listeners: list[Listener] = []

    @property
    def config(self) -> AppConfig:
        return self._config

    def subscribe(self, listener: Listener) -> None:
        """Register a callback invoked with the new config after every save."""
        self._listeners.append(listener)

    def _load(self) -> AppConfig:
        if not self._path.exists():
            cfg = AppConfig()
            self._save(cfg)
            return cfg
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return AppConfig.from_dict(raw)
        except (json.JSONDecodeError, OSError, KeyError, ValueError) as exc:
            logger.warning("Failed to load config (%s); falling back to defaults", exc)
            return AppConfig()

    def _save(self, cfg: AppConfig) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically so a crash mid-write never corrupts config.json.
        fd, tmp_name = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(cfg.to_dict(), f, indent=2)
            shutil.move(tmp_name, self._path)
        except OSError as exc:
            logger.error("Failed to persist config: %s", exc)
            Path(tmp_name).unlink(missing_ok=True)

    def save(self) -> None:
        self._save(self._config)
        for listener in self._listeners:
            listener(self._config)

    def replace(self, cfg: AppConfig) -> None:
        self._config = cfg
        self.save()
