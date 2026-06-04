"""Phase 21.3 — Cohort baseline state machine (Hard Rule 61).

Every project starts in a 7-day baseline window where injection is disabled
so we can measure the user's natural turn-count and output-token averages.
After the window closes we compute the baseline and enable injection.
Re-baseline every 90 days so the measurement stays current.

This is the integrity foundation of the money-back guarantee.
DO NOT skip the 7-day window for 'convenience' — the baseline IS the math.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

BASELINE_DURATION = timedelta(days=7)
REBASELINE_AFTER = timedelta(days=90)
REBASELINE_LENGTH = timedelta(days=3)

_DDL = """
CREATE TABLE IF NOT EXISTS cohort_baseline (
    project_id                TEXT PRIMARY KEY,
    baseline_started          TEXT NOT NULL,
    baseline_ended            TEXT,
    baseline_avg_turns        REAL,
    baseline_avg_output_tokens REAL,
    active_started            TEXT,
    last_recomputed           TEXT
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)
    conn.commit()


# ---------------------------------------------------------------------------
# State queries
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_in_baseline_window(project_id: str, conn: sqlite3.Connection) -> bool:
    """Return True when the project is in its baseline window (injection disabled)."""
    ensure_table(conn)
    row = conn.execute(
        "SELECT baseline_started, baseline_ended, last_recomputed FROM cohort_baseline WHERE project_id = ?",
        (project_id,),
    ).fetchone()

    if row is None:
        # First time we've seen this project — start a baseline window.
        _start_baseline(project_id, conn)
        return True

    baseline_started = datetime.fromisoformat(row[0])
    baseline_ended = row[1]
    last_recomputed = row[2]
    now = _now_utc()

    if baseline_ended is None:
        # Still in the initial baseline window.
        return True

    if last_recomputed:
        recomputed_at = datetime.fromisoformat(last_recomputed)
        if now - recomputed_at >= REBASELINE_AFTER:
            # Start a re-baseline window.
            _start_rebaseline(project_id, conn)
            return True

    return False


def days_remaining(project_id: str, conn: sqlite3.Connection) -> Optional[int]:
    """Days left in the current baseline window, or None if not in baseline."""
    ensure_table(conn)
    row = conn.execute(
        "SELECT baseline_started, baseline_ended FROM cohort_baseline WHERE project_id = ?",
        (project_id,),
    ).fetchone()

    if row is None or row[1] is not None:
        return None

    started = datetime.fromisoformat(row[0])
    elapsed = _now_utc() - started
    remaining = BASELINE_DURATION - elapsed
    if remaining.total_seconds() <= 0:
        return 0
    return max(0, remaining.days)


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def _start_baseline(project_id: str, conn: sqlite3.Connection) -> None:
    now = _now_utc().isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO cohort_baseline (project_id, baseline_started)
        VALUES (?, ?)
        """,
        (project_id, now),
    )
    conn.commit()


def _start_rebaseline(project_id: str, conn: sqlite3.Connection) -> None:
    now = _now_utc().isoformat()
    conn.execute(
        "UPDATE cohort_baseline SET baseline_started = ?, baseline_ended = NULL WHERE project_id = ?",
        (now, project_id),
    )
    conn.commit()


def compute_and_close_baseline(project_id: str, conn: sqlite3.Connection) -> None:
    """Compute baseline stats from the window and switch to active mode.

    Called at day 7 (or day 3 for re-baseline). Safe to call repeatedly —
    if baseline is already closed this is a no-op.
    """
    ensure_table(conn)
    row = conn.execute(
        "SELECT baseline_started, baseline_ended FROM cohort_baseline WHERE project_id = ?",
        (project_id,),
    ).fetchone()

    if row is None or row[1] is not None:
        return  # no open baseline or already closed

    window_start = row[0]
    now = _now_utc()

    # Compute from turns recorded during the baseline window.
    stats = conn.execute(
        """
        SELECT session_id,
               COUNT(*) AS turns,
               AVG(actual_output_tokens) AS avg_out
        FROM telemetry_session_timeline
        WHERE project_id = ?
          AND recorded_at BETWEEN ? AND ?
          AND baseline_period = 1
        GROUP BY session_id
        """,
        (project_id, window_start, now.isoformat()),
    ).fetchall()

    if stats:
        avg_turns = sum(r[1] for r in stats) / len(stats)
        total_weighted_out = sum((r[2] or 0) * r[1] for r in stats)
        total_turns = sum(r[1] for r in stats)
        avg_output = total_weighted_out / max(1, total_turns)
    else:
        avg_turns = 1.0
        avg_output = 200.0

    conn.execute(
        """
        UPDATE cohort_baseline
           SET baseline_ended              = ?,
               baseline_avg_turns         = ?,
               baseline_avg_output_tokens = ?,
               active_started             = ?,
               last_recomputed            = ?
         WHERE project_id = ?
        """,
        (
            now.isoformat(),
            round(avg_turns, 4),
            round(avg_output, 4),
            now.isoformat(),
            now.isoformat(),
            project_id,
        ),
    )
    conn.commit()


def get_baseline_stats(
    project_id: str,
    conn: sqlite3.Connection,
) -> tuple[float, float] | None:
    """Return (avg_turns, avg_output_tokens) or None if no completed baseline."""
    ensure_table(conn)
    row = conn.execute(
        "SELECT baseline_avg_turns, baseline_avg_output_tokens FROM cohort_baseline WHERE project_id = ? AND baseline_ended IS NOT NULL",
        (project_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return (float(row[0]), float(row[1]))
