"""SQLite database layer for the MemStrata MIT core.

Schema combines the base tables with migration 011 (V5.4 chat sessions) so the
server can be started against a fresh database without running separate migration
scripts. Plan-tier and Stripe-linkage tables are created by Pro overlay's
``memstrata_pro.pro_schema`` per the V5.2-E E.1 untangling.

DB path resolution order:
  1. ML_DB_PATH environment variable  (used for test isolation)
  2. ML_DATA_DIR environment variable / ".memstrata" subdirectory
  3. Default: ~/.memstrata/core.db
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import uuid
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# sqlite-vec extension loader
# ---------------------------------------------------------------------------

def _load_vec_extension(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec into *conn*. Returns True on success, False if unavailable.

    Must be called on every connection that will query telemetry_timeline_vec.
    Extension loading is cheap and idempotent — safe to call on every connection.
    """
    try:
        import sqlite_vec  # type: ignore[import]
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception as exc:
        _logger.warning("sqlite-vec not loadable: %s — install with: pip install sqlite-vec", exc)
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def get_db_path() -> Path:
    env = os.environ.get("ML_DB_PATH")
    if env:
        return Path(env)
    base_env = os.environ.get("ML_DATA_DIR")
    base = Path(base_env) if base_env else Path.home() / ".memstrata"
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
    -- Stream-pause dedup key: generated per DOM node by the browser extension.
    -- When non-NULL, (session_id, message_id) is unique so duplicate POSTs from
    -- a mid-stream pause are UPSERTed rather than inserted as duplicate rows.
    message_id            TEXT,
    -- Explicit client origin: 'browser_ext' | 'harness' | NULL (legacy).
    -- Used by the dashboard to split Chat vs Coding financials without
    -- relying on the brittle chat_session_id IS NOT NULL heuristic.
    client_source         TEXT,
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

-- Persisted settings (key-value). Open core uses this as a generic
-- store; Pro overlay seeds 'current_plan' for plan-tier gating.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- V5.2-E E.1: the Pro tables (`plan_features`, `stripe_customers`) and
-- the `current_plan` settings seed previously lived here. They moved to
-- `memstrata_pro/pro_schema.py`, applied at Pro overlay mount time.
"""


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables if they don't exist. Idempotent — safe to call on startup."""
    # WAL mode: allows concurrent reads while a write is in progress.
    # Persistent setting stored in the DB file — only needs to be set once.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    # Load sqlite-vec before running schema so _migrate_phase_34 can create vec0 table.
    _load_vec_extension(conn)
    conn.executescript(_SCHEMA)
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema. Safe to re-run — ignores existing columns."""
    new_cols = [
        "ALTER TABLE telemetry_session_timeline ADD COLUMN baseline_no_context INTEGER",
        "ALTER TABLE telemetry_session_timeline ADD COLUMN injected INTEGER DEFAULT 0",
        "ALTER TABLE telemetry_session_timeline ADD COLUMN cache_hit_estimated INTEGER DEFAULT 0",
        # Stream-pause dedup key (V5.3 fix): unique per DOM node within a session.
        "ALTER TABLE telemetry_session_timeline ADD COLUMN message_id TEXT",
        # Explicit client origin (V5.4 fix): 'browser_ext' | 'harness' | NULL (legacy).
        "ALTER TABLE telemetry_session_timeline ADD COLUMN client_source TEXT",
        # Phase 21 / Hard Rule 36 (three-baseline savings math): the harness
        # sends all three baselines per turn but earlier code only stored the
        # smallest (baseline_no_context), which inverted the input-savings
        # subtraction and produced $0 forever. These columns let the savings
        # formula use the LARGEST available baseline as the subtrahend.
        "ALTER TABLE telemetry_session_timeline ADD COLUMN baseline_full_repo INTEGER",
        "ALTER TABLE telemetry_session_timeline ADD COLUMN baseline_naive_rag INTEGER",
    ]
    for sql in new_cols:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists
    # Composite unique index for the message_id UPSERT path — CREATE UNIQUE INDEX
    # is idempotent with IF NOT EXISTS so safe to run on every startup.
    try:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_telemetry_message_id
                ON telemetry_session_timeline(session_id, message_id)
            """
        )
    except sqlite3.OperationalError:
        pass  # index already exists
    conn.commit()
    _migrate_phase_34(conn)


def _migrate_phase_34(conn: sqlite3.Connection) -> None:
    """Phase 34.1: embedding queue + vec0 table + backfill. Safe to re-run."""
    # embedding_queue: plain table, always safe to create.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embedding_queue (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timeline_id  INTEGER NOT NULL UNIQUE,
            enqueued_at  TEXT    DEFAULT (datetime('now')),
            attempts     INTEGER DEFAULT 0,
            last_error   TEXT,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_embedding_queue_pending
            ON embedding_queue(completed_at, attempts)
            WHERE completed_at IS NULL
        """
    )

    # telemetry_timeline_vec: vec0 virtual table — requires sqlite-vec loaded.
    # Skipped silently if sqlite-vec is unavailable; retrieval degrades gracefully.
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS telemetry_timeline_vec USING vec0(
                timeline_id  INTEGER PRIMARY KEY,
                embedding    float[768]
            )
            """
        )
    except sqlite3.OperationalError as exc:
        _logger.warning(
            "telemetry_timeline_vec creation skipped (sqlite-vec unavailable?): %s", exc
        )

    # Backfill: enqueue all existing timeline rows.
    # UNIQUE on timeline_id makes INSERT OR IGNORE idempotent — safe every startup.
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO embedding_queue (timeline_id)
            SELECT id FROM telemetry_session_timeline
            """
        )
    except sqlite3.OperationalError as exc:
        _logger.warning("Phase 34 backfill enqueue failed: %s", exc)

    conn.commit()
    _migrate_phase_35(conn)


def _migrate_phase_35(conn: sqlite3.Connection) -> None:
    """Phase 34.6: rewrite_telemetry table for per-rewrite quality tracking. Safe to re-run."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rewrite_telemetry (
            id                              INTEGER PRIMARY KEY AUTOINCREMENT,
            rewrite_id                      TEXT    NOT NULL UNIQUE,
            chat_session_id                 TEXT,
            external_session_id             TEXT,
            provider_id                     TEXT    NOT NULL,
            draft_prompt_chars              INTEGER NOT NULL,
            retrieved_turn_count            INTEGER NOT NULL DEFAULT 0,
            retrieved_turn_avg_similarity   REAL,
            retrieved_turn_age_dist_hours   TEXT,
            user_confirmed                  INTEGER NOT NULL,
            delimiter_format                TEXT    NOT NULL DEFAULT 'xml_tags',
            token_budget_used               INTEGER,
            token_budget_total              INTEGER,
            degraded                        INTEGER NOT NULL DEFAULT 0,
            degraded_reason                 TEXT,
            recorded_at                     TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    _migrate_phase_36_codebase(conn)


def _migrate_phase_36_codebase(conn: sqlite3.Connection) -> None:
    """Phase 36: codebase ingestion tables for the /context/injection endpoint.

    Two flat tables + one vec0 virtual table:
      codebase_files       (project_id, path) -> sha1, size, last_indexed
      codebase_chunks      one row per chunk, stores raw text + token count
      codebase_chunks_vec  vec0 virtual table holding the chunk embeddings

    Idempotent: CREATE ... IF NOT EXISTS makes re-running safe. The vec0 table
    is skipped silently when sqlite-vec is unavailable so the server still
    boots; ingest just won't be able to index.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS codebase_files (
            project_id    TEXT NOT NULL,
            path          TEXT NOT NULL,
            sha1          TEXT NOT NULL,
            size_bytes    INTEGER NOT NULL,
            token_count   INTEGER NOT NULL DEFAULT 0,
            last_indexed  TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (project_id, path)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_codebase_files_project
            ON codebase_files(project_id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS codebase_chunks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id   TEXT NOT NULL,
            path         TEXT NOT NULL,
            chunk_idx    INTEGER NOT NULL,
            text         TEXT NOT NULL,
            token_count  INTEGER NOT NULL,
            UNIQUE (project_id, path, chunk_idx)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_codebase_chunks_project
            ON codebase_chunks(project_id)
        """
    )
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS codebase_chunks_vec USING vec0(
                chunk_id   INTEGER PRIMARY KEY,
                embedding  float[768]
            )
            """
        )
    except sqlite3.OperationalError as exc:
        _logger.warning(
            "codebase_chunks_vec creation skipped (sqlite-vec unavailable?): %s", exc
        )
    conn.commit()
    _migrate_v5_2_a_code_chunks(conn)


def _migrate_v5_2_a_code_chunks(conn: sqlite3.Connection) -> None:
    """V5.2-A Phase 35.0 — automated codebase ingestion tables.

    Mirrors migrations/013_v5_2_a_code_chunks.sql verbatim; that file is
    the human-readable reference, this function is what actually runs
    at startup. Keep them in sync when either changes.

    Five tables:
      code_chunks_vec   vec0 virtual table holding semantic embeddings
      code_chunks       per-entity chunk metadata (line ranges, hashes)
      indexing_jobs     resume state for the backfill orchestrator (HR 72)
      project_opt_in    Hard Rule 70 gate; backfill never starts without it
      file_hashes       per-file content hash for branch-switch diffing
    """
    # code_chunks_vec — vec0 virtual; skip silently when sqlite-vec missing.
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS code_chunks_vec USING vec0(
                chunk_id   INTEGER PRIMARY KEY,
                embedding  float[768]
            )
            """
        )
    except sqlite3.OperationalError as exc:
        _logger.warning(
            "code_chunks_vec creation skipped (sqlite-vec unavailable?): %s", exc
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS code_chunks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id      TEXT NOT NULL,
            entity_id       TEXT,
            file_path       TEXT NOT NULL,
            language        TEXT NOT NULL,
            line_start      INTEGER NOT NULL,
            line_end        INTEGER NOT NULL,
            stable_hash     TEXT NOT NULL,
            text            TEXT NOT NULL,
            token_estimate  INTEGER NOT NULL DEFAULT 0,
            created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (project_id, file_path, line_start, line_end)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_code_chunks_project "
        "ON code_chunks (project_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_code_chunks_file "
        "ON code_chunks (project_id, file_path)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_code_chunks_hash "
        "ON code_chunks (project_id, stable_hash)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS indexing_jobs (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id          TEXT NOT NULL,
            project_path        TEXT NOT NULL,
            phase               TEXT NOT NULL CHECK (
                phase IN ('scan','parse','embed','verify','complete','paused','failed')
            ),
            files_total         INTEGER NOT NULL DEFAULT 0,
            files_processed     INTEGER NOT NULL DEFAULT 0,
            entities_total      INTEGER NOT NULL DEFAULT 0,
            entities_embedded   INTEGER NOT NULL DEFAULT 0,
            last_processed_file TEXT,
            started_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at        TIMESTAMP,
            error               TEXT,
            UNIQUE (project_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_indexing_jobs_phase "
        "ON indexing_jobs (phase)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_opt_in (
            project_path        TEXT PRIMARY KEY,
            state               TEXT NOT NULL CHECK (
                state IN ('opted_in','opted_out','pending')
            ),
            decided_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            user_added_dirs     TEXT,
            user_excluded_dirs  TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS file_hashes (
            project_id      TEXT NOT NULL,
            file_path       TEXT NOT NULL,
            content_hash    TEXT NOT NULL,
            last_seen       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (project_id, file_path)
        )
        """
    )

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
    *,
    increment_turn_count: bool = True,
) -> str:
    """Get-or-create a chat_sessions row; refresh last_seen and (optionally)
    increment turn_count.

    Returns the internal ``id`` (cs_ prefix) for use as FK in telemetry rows.

    Race-safety: uses INSERT OR IGNORE followed by an UPDATE so two concurrent
    callers with the same (provider_id, external_session_id) never produce
    duplicate rows. The UNIQUE constraint blocks the second INSERT; the UPDATE
    then runs against whichever row "won". SQLite serialises writes.

    `increment_turn_count=False` is used by the ingest path when the call is a
    stream-pause UPSERT update — i.e., the telemetry row already exists and is
    being rewritten with the final text. In that case the user-visible turn
    count must NOT advance, otherwise it inflates by 1 per re-fire.
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

    # Step 2 — UPDATE: always refresh last_seen; only bump turn_count when the
    # caller says this is a new logical turn.
    if increment_turn_count:
        conn.execute(
            """
            UPDATE chat_sessions
               SET last_seen  = datetime('now'),
                   turn_count = turn_count + 1
             WHERE provider_id = ? AND external_session_id = ?
            """,
            (provider_id, external_session_id),
        )
    else:
        conn.execute(
            """
            UPDATE chat_sessions
               SET last_seen = datetime('now')
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
    _load_vec_extension(conn)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 34 — embedding queue helpers
# ---------------------------------------------------------------------------

def enqueue_for_embedding(conn: sqlite3.Connection, timeline_id: int) -> None:
    """Insert *timeline_id* into the embedding queue.  No-op if already queued."""
    conn.execute(
        "INSERT OR IGNORE INTO embedding_queue (timeline_id) VALUES (?)",
        (timeline_id,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Phase 34 — shared datetime utility (§ Issue 3 decision)
# ---------------------------------------------------------------------------

def parse_recorded_at(s: str) -> datetime:
    """Parse telemetry_session_timeline.recorded_at → UTC-aware datetime.

    SQLite stores datetime('now') as '2026-06-05 02:42:23' (space separator, UTC).
    Python 3.11+ fromisoformat() accepts the space separator directly.
    We explicitly attach UTC so callers can safely subtract from datetime.now(UTC).
    """
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
