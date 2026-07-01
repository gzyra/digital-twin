"""Centralized configuration loader.

Every core module imports :func:`get_config` from here instead of opening
``config.yaml`` directly. The path is resolved from the ``DIGITAL_TWIN_CONFIG``
environment variable (falling back to ``config.yaml`` in the current directory),
so the app can be launched from any working directory.

The parsed config is cached; call :func:`reload_config` after editing the file
at runtime (requires an app restart in normal operation).
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

_CACHE: dict | None = None


def config_path() -> Path:
    """Return the resolved path to the active config file."""
    return Path(os.getenv("DIGITAL_TWIN_CONFIG", "config.yaml"))


def get_config() -> dict:
    """Return the parsed config, loading and caching it on first use."""
    global _CACHE
    if _CACHE is None:
        path = config_path()
        if not path.exists():
            raise FileNotFoundError(
                f"Missing config file: {path}. Create it from config.yaml.example."
            )
        with path.open("r", encoding="utf-8") as f:
            _CACHE = yaml.safe_load(f) or {}
    return _CACHE


def reload_config() -> dict:
    """Force a re-read of the config file and return the fresh dict."""
    global _CACHE
    _CACHE = None
    return get_config()
