"""IngestionService lifecycle — V5.2-A Phase 35.9.

One ``IngestionService`` per MIT core process. On ``start()`` it:

  1. Lists every ``project_opt_in`` row in ``state='opted_in'``.
  2. Per project: schedules a background branch-switch sweep AND
     constructs (and optionally starts) a ``CodebaseWatcher``.
  3. Wires the FastAPI lifespan so shutdown joins every thread.

Threading model:
  * Watchers each own a watchdog Observer (one OS thread) + a Python
    drain thread (one). Both spin off their own SQLite connection so
    the WAL writes don't trip Python's per-connection thread-affinity
    check.
  * Branch-switch sweeps run on a per-project worker thread with a
    dedicated connection of their own. The sweep completes once and
    the thread exits.
  * The service's own ``stop()`` is reentrant and idempotent so the
    FastAPI lifespan shutdown can call it without coordination tricks.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from memstrata.layer3._db import init_db
from memstrata.layer3.ingestion.branch_switch import (
    SweepResult,
    sweep_branch_switch,
)
from memstrata.layer3.ingestion.denylist import ProjectSkipPolicy
from memstrata.layer3.ingestion.resource_policy import ResourcePolicy
from memstrata.layer3.ingestion.watcher import (
    CodebaseWatcher,
    Embedder,
    NotOptedIn,
)

_LOG = logging.getLogger(__name__)


# ── Project record + factory hooks ────────────────────────────────────────

@dataclass
class ProjectRuntime:
    """Per-project bag of running things the service must clean up."""
    project_id: str
    project_path: str
    watcher: CodebaseWatcher | None = None
    sweep_thread: threading.Thread | None = None
    sweep_result: SweepResult | None = None
    sweep_error: str | None = None


WatcherFactory = Callable[[sqlite3.Connection, str, str], CodebaseWatcher]
SweepFactory = Callable[[sqlite3.Connection, str, str], SweepResult]


def _default_watcher_factory(
    conn: sqlite3.Connection,
    project_id: str,
    project_path: str,
    *,
    embedder: Embedder | None = None,
    skip_policy: ProjectSkipPolicy | None = None,
) -> CodebaseWatcher:
    return CodebaseWatcher(
        conn,
        project_id=project_id,
        project_root=project_path,
        embedder=embedder,
        skip_policy=skip_policy,
    )


def _default_sweep_factory(
    conn: sqlite3.Connection,
    project_id: str,
    project_path: str,
    *,
    embedder: Embedder | None = None,
    skip_policy: ProjectSkipPolicy | None = None,
) -> SweepResult:
    return sweep_branch_switch(
        conn, project_id, project_path,
        embedder=embedder, skip_policy=skip_policy,
    )


# ── Service ───────────────────────────────────────────────────────────────

class IngestionService:
    """V5.2-A Phase 35.9 — daemon-side ingestion supervisor."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        embedder: Embedder | None = None,
        resource_policy: ResourcePolicy | None = None,
        skip_policy: ProjectSkipPolicy | None = None,
        watcher_factory: Callable[..., CodebaseWatcher] | None = None,
        sweep_factory: Callable[..., SweepResult] | None = None,
        autostart_watchers: bool = True,
        autostart_sweeps: bool = True,
    ) -> None:
        self.db_path = str(db_path)
        self.embedder = embedder
        self.resource_policy = resource_policy or ResourcePolicy()
        self.skip_policy = skip_policy or ProjectSkipPolicy()
        self._watcher_factory = watcher_factory or _default_watcher_factory
        self._sweep_factory = sweep_factory or _default_sweep_factory
        self._autostart_watchers = autostart_watchers
        self._autostart_sweeps = autostart_sweeps

        self._lock = threading.Lock()
        self._projects: dict[str, ProjectRuntime] = {}
        self._started = False
        self._stopped = False

    # ── Public API ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Enumerate opted-in projects and wire each one up.

        Safe to call once. Subsequent calls are no-ops so the FastAPI
        lifespan handler (which can be invoked twice during test
        restarts) doesn't double-start watchers.
        """
        with self._lock:
            if self._started:
                return
            self._started = True

        for project in self._list_opted_in():
            try:
                self.add_project(project)
            except Exception as exc:                      # noqa: BLE001
                _LOG.warning(
                    "lifecycle: add_project failed for %s: %s",
                    project, exc,
                )

    def stop(self) -> None:
        """Join all sweep threads, stop all watchers. Idempotent."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            projects = list(self._projects.values())

        for runtime in projects:
            if runtime.watcher is not None:
                try:
                    runtime.watcher.stop()
                except Exception as exc:                  # noqa: BLE001
                    _LOG.debug("watcher stop raised: %s", exc)
            if runtime.sweep_thread is not None and runtime.sweep_thread.is_alive():
                runtime.sweep_thread.join(timeout=5.0)

    def add_project(self, project_path: str | Path) -> ProjectRuntime:
        """Begin sweep + watch for *project_path*.

        Idempotent: re-adding an already-running project returns the
        existing ``ProjectRuntime`` without starting a second watcher.
        """
        project_path_str = str(Path(project_path).resolve())
        with self._lock:
            if project_path_str in self._projects:
                return self._projects[project_path_str]

        project_id = self._project_id_from_path(project_path_str)
        # We open a fresh connection per project so concurrent watcher
        # threads don't trip sqlite3's check_same_thread default. WAL
        # mode is set inside init_db so writes don't block readers.
        conn = self._open_connection()
        try:
            self._ensure_project_opt_in(conn, project_path_str)
        except NotOptedIn as exc:
            conn.close()
            raise

        runtime = ProjectRuntime(
            project_id=project_id,
            project_path=project_path_str,
        )

        # ── Watcher ─────────────────────────────────────────────────────
        try:
            watcher = self._watcher_factory(
                conn, project_id, project_path_str,
                embedder=self.embedder,
                skip_policy=self.skip_policy,
            )
            runtime.watcher = watcher
            if self._autostart_watchers:
                watcher.start()
        except NotOptedIn:
            conn.close()
            raise
        except Exception as exc:                          # noqa: BLE001
            _LOG.warning("lifecycle: watcher construction failed: %s", exc)

        # ── Branch-switch sweep ────────────────────────────────────────
        if self._autostart_sweeps:
            sweep_conn = self._open_connection()
            sweep_thread = threading.Thread(
                target=self._run_sweep,
                args=(runtime, sweep_conn),
                name=f"branch-sweep-{project_id}",
                daemon=True,
            )
            runtime.sweep_thread = sweep_thread
            sweep_thread.start()

        with self._lock:
            self._projects[project_path_str] = runtime
        return runtime

    def remove_project(self, project_path: str | Path) -> None:
        """Stop watching *project_path*. Idempotent.

        Joins the sweep thread (if still running) so a subsequent
        add_project doesn't race against a stale connection.
        """
        project_path_str = str(Path(project_path).resolve())
        with self._lock:
            runtime = self._projects.pop(project_path_str, None)
        if runtime is None:
            return
        if runtime.watcher is not None:
            try:
                runtime.watcher.stop()
            except Exception as exc:                      # noqa: BLE001
                _LOG.debug("watcher stop raised on remove: %s", exc)
        if runtime.sweep_thread is not None and runtime.sweep_thread.is_alive():
            runtime.sweep_thread.join(timeout=5.0)

    # ── Read-only accessors (handy for the dashboard + tests) ──────────

    def get_project(self, project_path: str | Path) -> ProjectRuntime | None:
        with self._lock:
            return self._projects.get(str(Path(project_path).resolve()))

    def projects(self) -> list[ProjectRuntime]:
        with self._lock:
            return list(self._projects.values())

    # ── Internals ──────────────────────────────────────────────────────

    def _list_opted_in(self) -> list[str]:
        conn = self._open_connection()
        try:
            rows = conn.execute(
                "SELECT project_path FROM project_opt_in WHERE state = 'opted_in'"
            ).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]

    def _ensure_project_opt_in(self, conn: sqlite3.Connection, project_path: str) -> None:
        row = conn.execute(
            "SELECT state FROM project_opt_in WHERE project_path = ?",
            (project_path,),
        ).fetchone()
        if row is None or row[0] != "opted_in":
            raise NotOptedIn(
                f"Hard Rule 73: IngestionService refused to add {project_path!r} "
                f"— project_opt_in.state is {row[0] if row else 'missing'}"
            )

    def _project_id_from_path(self, path: str) -> str:
        """Stable, deterministic id derived from the absolute path.

        Production callers pass an id assigned by their own metadata
        store; this default makes single-tenant local dev seamless and
        keeps the registry / DB rows linkable in tests.
        """
        return f"mlp:{Path(path).name}:{abs(hash(path)) & 0xFFFFFFFF:08x}"

    def _open_connection(self) -> sqlite3.Connection:
        # check_same_thread=False because watcher + sweep threads share
        # the connection's WAL writer. Workers are funneled through
        # internal queues + the per-call commits already; SQLite's WAL
        # mode (set by init_db) handles concurrent readers cleanly.
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        init_db(conn)
        return conn

    def _run_sweep(self, runtime: ProjectRuntime, conn: sqlite3.Connection) -> None:
        try:
            runtime.sweep_result = self._sweep_factory(
                conn, runtime.project_id, runtime.project_path,
                embedder=self.embedder,
                skip_policy=self.skip_policy,
            )
        except Exception as exc:                          # noqa: BLE001
            runtime.sweep_error = str(exc)
            _LOG.warning(
                "lifecycle: branch-switch sweep failed for %s: %s",
                runtime.project_path, exc,
            )
        finally:
            try:
                conn.close()
            except Exception:                             # noqa: BLE001
                pass
