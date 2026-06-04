"""SQLite database layer for the Memory Layer MIT core.

Schema combines the base tables with migration 011 (V5.4 chat sessions) so the
server can be started against a fresh database without running separate migration
scripts.  The billing conftest already mirrors this schema for financial tests.

DB path resolution order:
  1. ML_DB_PATH environment variable  (used for test isolation)
  2. ML_DATA_DIR environment variable / ".memory-layer" subdirectory
  3. Default: ~/.memory-layer/core.db
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Generator

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def get_db_path() -> Path:
    env = os.environ.get("ML_DB_PATH")
    if env:
        return Path(env)
    base_env = os.environ.get("ML_DATA_DIR")
    base = Path(base_env) if base_env else Path.home() / ".memory-layer"
    base.mkdir(parents=True, exist_ok=True)
    return base / "core.db"


# ---------------------------------------------------------------------------
# Schema (base tables + migration 011 inline)
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    closed_at    TEXT,
    context_hash TEXT,
    client_id    TEXT
);

-- Migration 011: hierarchical per-provider chat session storage (V5.4 §2.1)
CREATE TABLE IF NOT EXISTS chat_sessions (
    id                  TEXT    PRIMARY KEY,
    provider_id         TEXT    NOT NULL,
    external_session_id TEXT    NOT NULL,
    title               TEXT,
    first_seen          TEXT    DEFAULT (datetime('now')),
    last_seen           TEXT    DEFAULT (datetime('now')),
    turn_count          INTEGER DEFAULT 0,
    UNIQUE (provider_id, external_session_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_provider
    ON chat_sessions(provider_id);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_last_seen
    ON chat_sessions(last_seen DESC);

CREATE TABLE IF NOT EXISTS telemetry_session_timeline (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            TEXT    NOT NULL,
    turn_id               INTEGER NOT NULL,
    project_id            TEXT    NOT NULL,
    user_id               TEXT,
    provider              TEXT,
    model                 TEXT,
    actual_input_tokens   INTEGER DEFAULT 0,
    actual_output_tokens  INTEGER DEFAULT 0,
    saved_input_cost_usd  REAL    DEFAULT 0.0,
    saved_cache_cost_usd  REAL    DEFAULT 0.0,
    saved_output_cost_usd REAL    DEFAULT 0.0,
    measurement_basis     TEXT    DEFAULT 'input_measured',
    baseline_period       INTEGER DEFAULT 0,
    recorded_at           TEXT    DEFAULT (datetime('now')),
    -- V5.4 migration 011: link each turn to a provider chat session
    chat_session_id       TEXT    REFERENCES chat_sessions(id),
    -- Browser extension captured turn content
    role                  TEXT,
    text                  TEXT,
    char_count            INTEGER,
    -- Phase 32: harness baseline and injection metadata for per-thread savings
    baseline_no_context   INTEGER,
    injected              INTEGER DEFAULT 0,
    cache_hit_estimated   INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_telemetry_chat
    ON telemetry_session_timeline(chat_session_id);

CREATE TABLE IF NOT EXISTS rationale_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT,
    project_id      TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    content         TEXT,
    -- V5.4 migration 011
    chat_session_id TEXT REFERENCES chat_sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_rationale_log_chat
    ON rationale_log(chat_session_id);

-- Phase 21: dynamic pricing fetched from OpenRouter
CREATE TABLE IF NOT EXISTS provider_pricing (
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    input_per_m     REAL NOT NULL,
    output_per_m    REAL NOT NULL,
    cache_write_per_m REAL,
    cache_read_per_m  REAL,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (provider, model)
);

-- Phase 33: two-tier billing — plan feature flags
CREATE TABLE IF NOT EXISTS plan_features (
    plan     TEXT PRIMARY KEY,
    features TEXT NOT NULL  -- JSON array of feature flag strings
);

INSERT OR IGNORE INTO plan_features VALUES
  ('free',  '["mcp_server", "local_dashboard"]'),
  ('trial', '["mcp_server", "local_dashboard", "browser_ext", "harness", "vscode_ext", "money_tab"]'),
  ('lite',  '["mcp_server", "local_dashboard", "browser_ext", "money_tab_chat_only"]'),
  ('pro',   '["mcp_server", "local_dashboard", "browser_ext", "harness", "vscode_ext", "money_tab"]'),
  ('team',  '["mcp_server", "local_dashboard", "browser_ext", "harness", "vscode_ext", "money_tab", "team_sync", "shared_dashboard"]');

-- Persisted settings (key-value). current_plan defaults to 'trial' for all
-- existing users so they retain full access while the billing rollout happens.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO settings (key, value) VALUES ('current_plan', 'trial');
"""


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables if they don't exist. Idempotent — safe to call on startup."""
    # WAL mode: allows concurrent reads while a write is in progress.
    # Persistent setting stored in the DB file — only needs to be set once.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.executescript(_SCHEMA)
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema. Safe to re-run — ignores existing columns."""
    new_cols = [
        "ALTER TABLE telemetry_session_timeline ADD COLUMN baseline_no_context INTEGER",
        "ALTER TABLE telemetry_session_timeline ADD COLUMN injected INTEGER DEFAULT 0",
        "ALTER TABLE telemetry_session_timeline ADD COLUMN cache_hit_estimated INTEGER DEFAULT 0",
    ]
    for sql in new_cols:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()


# ---------------------------------------------------------------------------
# External session ID validation
# ---------------------------------------------------------------------------

# Matches the union of all provider URL-session-ID patterns:
#   - ChatGPT/Claude/Gemini: hex UUID  e.g. 3f4e5d6c-7b8a-9012-cdef-3456789abcde
#   - Meta.ai: numeric          e.g. 123456789
#   - Perplexity/Grok/Mistral:  alphanumeric+hyphen e.g. 4fb8pRzqKqGPnmSy
# Max 256 chars — provider IDs are never longer in practice.
_VALID_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,256}$")


def is_valid_external_session_id(value: str) -> bool:
    """Return True iff *value* is safe to use as an external_session_id key.

    Rejects empty strings, whitespace-only values, values exceeding 256 chars,
    and any value containing characters outside [A-Za-z0-9_.-].
    This blocks SQL/path injection while accepting every known provider ID format.
    """
    return bool(_VALID_SESSION_ID_RE.match(value))


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def new_id(prefix: str = "id") -> str:
    """Generate a short unique ID with a type prefix (e.g. 'cs_a1b2c3d4e5f6a7b8')."""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# chat_sessions upsert (core of Phase 31)
# ---------------------------------------------------------------------------

def upsert_chat_session(
    conn: sqlite3.Connection,
    provider_id: str,
    external_session_id: str,
) -> str:
    """Get-or-create a chat_sessions row; increment turn_count and refresh last_seen.

    Returns the internal ``id`` (cs_ prefix) for use as FK in telemetry rows.

    Race-safety: uses INSERT OR IGNORE followed by an unconditional UPDATE so
    that two concurrent callers with the same (provider_id, external_session_id)
    never produce duplicate rows.  The UNIQUE constraint prevents the second
    INSERT from creating a duplicate; the UPDATE then increments turn_count on
    whichever row "won".  SQLite serialises writes, so each caller ultimately
    sees exactly one row.
    """
    candidate_id = new_id("cs")

    # Step 1 — try to insert; silently skip if the row already exists.
    conn.execute(
        """
        INSERT OR IGNORE INTO chat_sessions (id, provider_id, external_session_id, turn_count)
        VALUES (?, ?, ?, 0)
        """,
        (candidate_id, provider_id, external_session_id),
    )

    # Step 2 — unconditional UPDATE: runs whether we just inserted or not.
    # This is the only statement that modifies turn_count, so counts are accurate
    # under concurrent writes.
    conn.execute(
        """
        UPDATE chat_sessions
           SET last_seen  = datetime('now'),
               turn_count = turn_count + 1
         WHERE provider_id = ? AND external_session_id = ?
        """,
        (provider_id, external_session_id),
    )

    # Step 3 — read back the canonical ID (may differ from candidate_id if the
    # INSERT was ignored because another writer raced us).
    row = conn.execute(
        "SELECT id FROM chat_sessions WHERE provider_id = ? AND external_session_id = ?",
        (provider_id, external_session_id),
    ).fetchone()

    conn.commit()
    return row[0]


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

def get_conn() -> Generator[sqlite3.Connection, None, None]:
    """Yield a per-request SQLite connection; close after the handler returns."""
    conn = sqlite3.connect(str(get_db_path()), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Serialize concurrent writers up to 10 s before raising OperationalError.
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        yield conn
    finally:
        conn.close()
