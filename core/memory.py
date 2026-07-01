"""Persistent skill result cache.

Results are stored as JSON files under memory_dir (default: memory/).
Each file is named <skill_name>.json and holds the captured outputs plus
metadata (capture timestamp, skill name).

This module is intentionally free of Chainlit/async dependencies so it
can be imported anywhere — app.py, CLI scripts, future LLM tools.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from core.config import get_config

_CFG = get_config()

MEMORY_DIR = Path(_CFG.get("memory_dir", "memory"))
MEMORY_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_TTL_HOURS: float = float(_CFG.get("startup_skill_ttl_hours", 24))


# ---------------------------------------------------------------------------
# Core read / write
# ---------------------------------------------------------------------------

def _path(skill_name: str) -> Path:
    return MEMORY_DIR / f"{skill_name}.json"


def save_result(skill_name: str, outputs: dict) -> None:
    """Persist skill outputs to disk, overwriting any previous result."""
    data = {
        "skill_name": skill_name,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "outputs": outputs,
    }
    with open(_path(skill_name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_result(skill_name: str) -> dict | None:
    """Load the cached result for a skill, or None if not found / unreadable."""
    path = _path(skill_name)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def is_stale(skill_name: str, max_age_hours: float | None = None) -> bool:
    """Return True when there is no cached result or it is older than max_age_hours."""
    hours = max_age_hours if max_age_hours is not None else DEFAULT_TTL_HOURS
    data = load_result(skill_name)
    if not data:
        return True
    try:
        captured = datetime.fromisoformat(data["captured_at"])
        age_seconds = (datetime.now(timezone.utc) - captured).total_seconds()
        return age_seconds > hours * 3600
    except Exception:
        return True


def age_description(skill_name: str) -> str:
    """Human-readable age of the cached result, e.g. '3h 12m ago'."""
    data = load_result(skill_name)
    if not data:
        return "no cache"
    try:
        captured = datetime.fromisoformat(data["captured_at"])
        secs = int((datetime.now(timezone.utc) - captured).total_seconds())
        h, rem = divmod(secs, 3600)
        m = rem // 60
        if h:
            return f"{h}h {m}m ago"
        return f"{m}m ago"
    except Exception:
        return "unknown age"


# ---------------------------------------------------------------------------
# Listing / inspection
# ---------------------------------------------------------------------------

def list_results() -> list[dict]:
    """Return metadata for all stored results, sorted by capture time (newest first)."""
    results = []
    for f in MEMORY_DIR.glob("*.json"):
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            outputs = data.get("outputs", {})
            results.append({
                "skill_name": data.get("skill_name", f.stem),
                "captured_at": data.get("captured_at", ""),
                "output_keys": list(outputs.keys()),
                "stale": is_stale(data.get("skill_name", f.stem)),
            })
        except Exception:
            pass
    results.sort(key=lambda r: r.get("captured_at", ""), reverse=True)
    return results


def all_outputs_for_llm(max_value_chars: int = 2000) -> str:
    """Return a compact text block of all stored outputs suitable for an LLM system prompt.

    Table values are summarised as row counts; long strings are truncated.
    This keeps the prompt size bounded while giving the LLM enough context
    to answer questions and decide which skills to trigger.
    """
    results = list_results()
    if not results:
        return ""

    lines: list[str] = ["## Stored skill results (from memory):"]
    for meta in results:
        skill = meta["skill_name"]
        age = age_description(skill)
        data = load_result(skill)
        if not data:
            continue
        lines.append(f"\n### {skill}  _(captured {age})_")
        for key, value in data.get("outputs", {}).items():
            if not isinstance(value, str) or not value.strip():
                continue
            table_lines = [l for l in value.split("\n") if l.strip().startswith("|")]
            if len(table_lines) >= 3:
                # Summarise table: include headers + first 5 data rows
                header = table_lines[0] if table_lines else ""
                data_rows = [l for l in table_lines[2:]][:5]
                preview = "\n".join([header, table_lines[1]] + data_rows)
                total_rows = max(0, len(table_lines) - 2)
                lines.append(
                    f"**{key}** — table with {total_rows} rows "
                    f"(preview of first 5):\n```\n{preview}\n```"
                )
            else:
                snippet = value[:max_value_chars]
                if len(value) > max_value_chars:
                    snippet += " …[truncated]"
                lines.append(f"**{key}**: {snippet}")

    return "\n".join(lines)
