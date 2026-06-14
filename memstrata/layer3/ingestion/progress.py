"""Progress tracking + snapshot helpers — V5.2-A Phase 35.3.

The orchestrator owns the actual indexing_jobs row; this module's job
is to translate that row into a UI-friendly snapshot:

  * Per-phase progress (counts + percentages)
  * Current file being processed
  * ETA based on running-average throughput since started_at
  * Pause / resume / cancel state

Plus a process-local registry that maps project_id -> ``ControlState`` so
the API endpoints can pause / resume / cancel a running orchestrator
from a different thread without touching its instance.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ── Process-local control flags ────────────────────────────────────────────

@dataclass
class ControlState:
    """Inter-thread signal block for one in-flight indexing job."""
    pause_flag: threading.Event = field(default_factory=threading.Event)
    cancel_flag: threading.Event = field(default_factory=threading.Event)
    # Resource policy can set a soft-pause reason without flipping the
    # full pause_flag — used so the UI can show "paused: on-battery"
    # without persisting a paused phase to the DB.
    soft_pause_reason: str | None = None
    # Set whenever the orchestrator finishes a phase or batch; lets the
    # API endpoint block on "wait until paused" if it ever needs to.
    progress_event: threading.Event = field(default_factory=threading.Event)


class ControlRegistry:
    """Thread-safe project_id -> ControlState dict.

    A module-level singleton instance lives at the bottom of this file
    (``CONTROL_REGISTRY``); the orchestrator looks it up by project_id.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, ControlState] = {}

    def get_or_create(self, project_id: str) -> ControlState:
        with self._lock:
            state = self._states.get(project_id)
            if state is None:
                state = ControlState()
                self._states[project_id] = state
            return state

    def get(self, project_id: str) -> ControlState | None:
        with self._lock:
            return self._states.get(project_id)

    def drop(self, project_id: str) -> None:
        with self._lock:
            self._states.pop(project_id, None)


CONTROL_REGISTRY = ControlRegistry()


# ── ETA + snapshot ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProgressSnapshot:
    """Serializable view of an indexing_jobs row + control state."""
    project_id: str
    project_path: str
    phase: str
    files_total: int
    files_processed: int
    entities_total: int
    entities_embedded: int
    last_processed_file: str | None
    started_at: str
    completed_at: str | None
    error: str | None
    # Derived fields:
    files_pct: float            # 0..1
    entities_pct: float         # 0..1
    elapsed_seconds: float
    eta_seconds: float | None
    rate_files_per_second: float | None
    rate_entities_per_second: float | None
    is_paused: bool
    is_cancelling: bool
    soft_pause_reason: str | None

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "project_path": self.project_path,
            "phase": self.phase,
            "files_total": self.files_total,
            "files_processed": self.files_processed,
            "entities_total": self.entities_total,
            "entities_embedded": self.entities_embedded,
            "last_processed_file": self.last_processed_file,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "files_pct": self.files_pct,
            "entities_pct": self.entities_pct,
            "elapsed_seconds": self.elapsed_seconds,
            "eta_seconds": self.eta_seconds,
            "rate_files_per_second": self.rate_files_per_second,
            "rate_entities_per_second": self.rate_entities_per_second,
            "is_paused": self.is_paused,
            "is_cancelling": self.is_cancelling,
            "soft_pause_reason": self.soft_pause_reason,
        }


def _parse_db_timestamp(value: str) -> datetime | None:
    """SQLite default `CURRENT_TIMESTAMP` lays down strings like
    '2026-06-07 04:32:11'. ISO-8601 with 'T' separator works too."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _safe_pct(num: float, denom: float) -> float:
    if denom <= 0:
        return 0.0
    return max(0.0, min(1.0, num / denom))


def build_snapshot(
    row: dict,
    *,
    now: datetime | None = None,
    control: ControlState | None = None,
) -> ProgressSnapshot:
    """Translate an indexing_jobs row dict into a UI snapshot.

    ``row`` keys correspond exactly to the table columns. ``now`` is
    overridable for deterministic tests.
    """
    now = now or datetime.now(timezone.utc)
    started_dt = _parse_db_timestamp(str(row.get("started_at") or ""))
    elapsed = (now - started_dt).total_seconds() if started_dt else 0.0
    elapsed = max(elapsed, 0.0)

    files_total = int(row.get("files_total") or 0)
    files_processed = int(row.get("files_processed") or 0)
    entities_total = int(row.get("entities_total") or 0)
    entities_embedded = int(row.get("entities_embedded") or 0)

    # Running-average rates from started_at to now. We deliberately use
    # elapsed_seconds (not since-last-tick) so a stall doesn't make ETA
    # spike erratically — the running average dampens it.
    rate_files = (files_processed / elapsed) if elapsed > 0.5 else None
    rate_entities = (entities_embedded / elapsed) if elapsed > 0.5 else None

    # ETA is "how long to finish the slowest still-pending phase". We
    # take the max of files-remaining and entities-remaining wall times
    # so the user sees the longer of the two as the truth.
    eta: float | None = None
    if row.get("phase") in ("parse", "embed"):
        candidates: list[float] = []
        if rate_files and files_total > files_processed:
            candidates.append((files_total - files_processed) / rate_files)
        if rate_entities and entities_total > entities_embedded:
            candidates.append((entities_total - entities_embedded) / rate_entities)
        if candidates:
            eta = max(candidates)

    is_paused = bool(control.pause_flag.is_set()) if control else False
    is_cancelling = bool(control.cancel_flag.is_set()) if control else False
    soft_pause_reason = control.soft_pause_reason if control else None
    # The persisted 'paused' phase should also be reflected as paused.
    if row.get("phase") == "paused":
        is_paused = True

    return ProgressSnapshot(
        project_id=str(row.get("project_id") or ""),
        project_path=str(row.get("project_path") or ""),
        phase=str(row.get("phase") or ""),
        files_total=files_total,
        files_processed=files_processed,
        entities_total=entities_total,
        entities_embedded=entities_embedded,
        last_processed_file=row.get("last_processed_file"),
        started_at=str(row.get("started_at") or ""),
        completed_at=row.get("completed_at"),
        error=row.get("error"),
        files_pct=_safe_pct(files_processed, files_total),
        entities_pct=_safe_pct(entities_embedded, entities_total),
        elapsed_seconds=elapsed,
        eta_seconds=eta,
        rate_files_per_second=rate_files,
        rate_entities_per_second=rate_entities,
        is_paused=is_paused,
        is_cancelling=is_cancelling,
        soft_pause_reason=soft_pause_reason,
    )
