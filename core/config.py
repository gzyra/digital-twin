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


def get_enum_for_parameter(param_name: str) -> list[str] | None:
    """Get enum/picklist options for a parameter from config if defined.
    
    Maps parameter names to config.yaml picklists. Returns None if no enum defined.
    
    Args:
        param_name: Parameter name (e.g., "sales_level", "business_entity")
    
    Returns:
        List of strings (picklist options) or None if not found.
    
    Example:
        get_enum_for_parameter("sales_level")      # → ["Americas", "EMEA", ...]
        get_enum_for_parameter("business_entity")  # → ["Collaboration", ...]
        get_enum_for_parameter("unknown")          # → None
    """
    config = get_config()
    
    # Map parameter names to config.yaml paths: (section, key)
    enum_mappings = {
        "sales_level": ("cxaia", "sales_levels"),
        "time_frame": ("cxaia", "time_frames"),
        "business_entity": ("cxaia", "business_entities"),
        "fiscal_period": ("cxaia", "fiscal_periods"),
        "sales_region": ("cxaia", "sales_regions"),
        "sales_theater": ("cxaia", "sales_theaters"),
    }
    
    if param_name not in enum_mappings:
        return None
    
    section, key = enum_mappings[param_name]
    return config.get(section, {}).get(key)
