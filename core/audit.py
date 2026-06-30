"""SQLite audit log for every skill execution.

Every run — successful or failed — is written to audit.db so that:
- operators can review what ran, when, and with what parameters,
- failures can be investigated post-hoc,
- a user can ask "what did the agent do?" and get a real answer.

Schema (single table):
  executions(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name  TEXT NOT NULL,
    display_name TEXT,
    params      TEXT,          -- JSON-encoded dict of user-supplied params
    started_at  TEXT NOT NULL, -- ISO-8601 UTC
    ended_at    TEXT,          -- ISO-8601 UTC; NULL while running
    duration_s  REAL,          -- wall-clock seconds; NULL while running
    status      TEXT,          -- 'running' | 'success' | 'stopped' | 'error'
    error       TEXT,          -- error message if status='error'
    outputs     TEXT           -- JSON-encoded dict of captured outputs
  )
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_db_path() -> Path:
    cfg_path = Path(os.getenv("DIGITAL_TWIN_CONFIG", "config.yaml"))
    if cfg_path.exists():
        with cfg_path.open() as f:
            cfg = yaml.safe_load(f) or {}
        db_file = cfg.get("audit_db", "audit.db")
    else:
        db_file = "audit.db"
    return Path(db_file)


DB_PATH: Path = _load_db_path()


# ---------------------------------------------------------------------------
# DB bootstrap
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # safer for concurrent access
    conn.execute("PRAGMA synchronous=NORMAL") # balance safety vs speed
    return conn


@contextmanager
def _cursor():
    conn = _connect()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create the executions table if it doesn't exist. Safe to call on every startup."""
    with _cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS executions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_name   TEXT    NOT NULL,
                display_name TEXT,
                params       TEXT,
                started_at   TEXT    NOT NULL,
                ended_at     TEXT,
                duration_s   REAL,
                status       TEXT    NOT NULL DEFAULT 'running',
                error        TEXT,
                outputs      TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_exec_started ON executions(started_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_exec_skill   ON executions(skill_name)")


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def log_start(skill_name: str, display_name: str = "", params: dict | None = None) -> int:
    """Insert a 'running' record and return its row id.

    Call this before launching the skill so that crashes are still recorded.
    """
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO executions (skill_name, display_name, params, started_at, status)
            VALUES (?, ?, ?, ?, 'running')
            """,
            (
                skill_name,
                display_name or skill_name,
                json.dumps(params or {}, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid  # type: ignore[return-value]


def log_end(
    run_id: int,
    *,
    status: str = "success",
    outputs: dict | None = None,
    error: str | None = None,
) -> None:
    """Update an existing run record with completion details.

    status: 'success' | 'stopped' | 'error'
    """
    ended = datetime.now(timezone.utc)

    with _cursor() as cur:
        # Fetch started_at to compute duration
        cur.execute("SELECT started_at FROM executions WHERE id = ?", (run_id,))
        row = cur.fetchone()
        duration: float | None = None
        if row:
            try:
                started = datetime.fromisoformat(row["started_at"])
                duration = (ended - started).total_seconds()
            except Exception:
                pass

        cur.execute(
            """
            UPDATE executions
               SET ended_at   = ?,
                   duration_s = ?,
                   status     = ?,
                   error      = ?,
                   outputs    = ?
             WHERE id = ?
            """,
            (
                ended.isoformat(),
                duration,
                status,
                error,
                json.dumps(outputs or {}, ensure_ascii=False),
                run_id,
            ),
        )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def recent_runs(limit: int = 20, skill_name: str | None = None) -> list[dict]:
    """Return the most recent executions as plain dicts, newest first."""
    with _cursor() as cur:
        if skill_name:
            cur.execute(
                "SELECT * FROM executions WHERE skill_name = ? ORDER BY started_at DESC LIMIT ?",
                (skill_name, limit),
            )
        else:
            cur.execute(
                "SELECT * FROM executions ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
        rows = cur.fetchall()

    results = []
    for row in rows:
        d = dict(row)
        # Deserialise JSON blobs
        for key in ("params", "outputs"):
            try:
                d[key] = json.loads(d[key]) if d[key] else {}
            except Exception:
                d[key] = {}
        results.append(d)
    return results


def format_audit_summary(runs: list[dict], max_rows: int = 10) -> str:
    """Return a markdown table of recent runs suitable for chat display."""
    if not runs:
        return "_No executions recorded yet._"

    header = "| # | Skill | Status | Duration | Started |\n|---|---|---|---|---|"
    lines = [header]

    STATUS_EMOJI = {"success": "✅", "error": "❌", "stopped": "⏹️", "running": "⏳"}

    for i, run in enumerate(runs[:max_rows], 1):
        emoji = STATUS_EMOJI.get(run.get("status", ""), "❓")
        name = run.get("display_name") or run.get("skill_name", "?")
        status = f"{emoji} {run.get('status', '?')}"
        dur = run.get("duration_s")
        dur_str = f"{dur:.1f}s" if dur is not None else "—"
        started = (run.get("started_at") or "")[:16].replace("T", " ")
        lines.append(f"| {i} | {name} | {status} | {dur_str} | {started} |")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Initialise on import
# ---------------------------------------------------------------------------

init_db()
