"""Tests for V5.2-A Phase 35.9 — IngestionService lifecycle."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from memstrata.layer3._db import init_db
from memstrata.layer3.ingestion import (
    IngestionService,
    NoOpEmbedder,
    ProjectRuntime,
    SweepResult,
)
from memstrata.layer3.ingestion.orchestrator import record_opt_in
from memstrata.layer3.ingestion.watcher import NotOptedIn

# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    p = tmp_path / "core.db"
    monkeypatch.setenv("ML_DB_PATH", str(p))
    # Pre-create the schema so the service's connection factory finds it.
    c = sqlite3.connect(str(p))
    init_db(c)
    c.close()
    return p


def _seed_python_file(root: Path, name: str, *, num_funcs: int = 2) -> Path:
    f = root / name
    f.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "# header\n" * 45
        + "\n".join(
            f"def fn_{i}(a, b):\n    return a + b + {i}\n"
            for i in range(num_funcs)
        )
        + "\n"
    )
    f.write_text(body, encoding="utf-8")
    return f


def _record_opt_in(db_path: Path, project_path: Path, state: str = "opted_in") -> None:
    c = sqlite3.connect(str(db_path))
    record_opt_in(c, project_path, state=state)
    c.close()


# ── Watcher / sweep factories used by tests so we don't spawn real
#    watchdog Observers or run actual SHA-256 walks during unit tests. ────

class _MockWatcher:
    started = False
    stopped = False

    def __init__(self, conn, project_id, project_path, **kwargs):
        self.conn = conn
        self.project_id = project_id
        self.project_path = project_path
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def _mock_watcher_factory(conn, project_id, project_path, **kwargs):
    return _MockWatcher(conn, project_id, project_path, **kwargs)


def _stub_sweep_factory(conn, project_id, project_path, **kwargs) -> SweepResult:
    return SweepResult(unchanged_files=0)


# ── start() / list_opted_in ──────────────────────────────────────────────

class TestStart:
    def test_start_enumerates_only_opted_in_projects(self, db_path, tmp_path):
        opted = tmp_path / "opted"
        opted.mkdir()
        pending = tmp_path / "pending"
        pending.mkdir()
        out = tmp_path / "out"
        out.mkdir()
        _record_opt_in(db_path, opted, "opted_in")
        _record_opt_in(db_path, pending, "pending")
        _record_opt_in(db_path, out, "opted_out")

        svc = IngestionService(
            db_path, watcher_factory=_mock_watcher_factory,
            sweep_factory=_stub_sweep_factory,
        )
        svc.start()
        try:
            paths = {r.project_path for r in svc.projects()}
            # Resolve so the comparison is symlink/casing-stable.
            assert str(opted.resolve()) in paths
            assert str(pending.resolve()) not in paths
            assert str(out.resolve()) not in paths
        finally:
            svc.stop()

    def test_start_is_idempotent(self, db_path, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        _record_opt_in(db_path, proj)
        svc = IngestionService(
            db_path, watcher_factory=_mock_watcher_factory,
            sweep_factory=_stub_sweep_factory,
        )
        svc.start()
        svc.start()    # must not raise / must not start the watcher twice
        try:
            assert len(svc.projects()) == 1
            assert svc.get_project(proj).watcher.started is True
        finally:
            svc.stop()

    def test_start_no_op_when_disabled(self, db_path, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        _record_opt_in(db_path, proj)
        svc = IngestionService(
            db_path,
            watcher_factory=_mock_watcher_factory,
            sweep_factory=_stub_sweep_factory,
            autostart_watchers=False,
            autostart_sweeps=False,
        )
        svc.start()
        try:
            runtime = svc.get_project(proj)
            assert runtime is not None
            # Watcher constructed but never .start()ed.
            assert runtime.watcher.started is False
            # No sweep thread spawned.
            assert runtime.sweep_thread is None
        finally:
            svc.stop()


# ── add_project / remove_project ────────────────────────────────────────

class TestAddRemoveProject:
    def test_add_project_refuses_non_opted_in(self, db_path, tmp_path):
        proj = tmp_path / "stranger"
        proj.mkdir()
        svc = IngestionService(
            db_path, watcher_factory=_mock_watcher_factory,
            sweep_factory=_stub_sweep_factory,
        )
        with pytest.raises(NotOptedIn):
            svc.add_project(proj)
        svc.stop()

    def test_add_project_is_idempotent(self, db_path, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        _record_opt_in(db_path, proj)
        svc = IngestionService(
            db_path, watcher_factory=_mock_watcher_factory,
            sweep_factory=_stub_sweep_factory,
        )
        first = svc.add_project(proj)
        second = svc.add_project(proj)
        try:
            assert first is second
            assert len(svc.projects()) == 1
        finally:
            svc.stop()

    def test_remove_project_stops_watcher_and_joins_sweep(self, db_path, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        _seed_python_file(proj, "a.py")
        _record_opt_in(db_path, proj)
        svc = IngestionService(
            db_path, watcher_factory=_mock_watcher_factory,
            sweep_factory=_stub_sweep_factory,
        )
        runtime = svc.add_project(proj)
        watcher = runtime.watcher
        svc.remove_project(proj)
        assert watcher.stopped is True
        assert svc.get_project(proj) is None
        # Subsequent removal is a safe no-op.
        svc.remove_project(proj)

    def test_remove_unknown_project_is_safe(self, db_path, tmp_path):
        svc = IngestionService(db_path, watcher_factory=_mock_watcher_factory)
        svc.remove_project(tmp_path / "ghost")     # must not raise


# ── stop() cleanup ──────────────────────────────────────────────────────

class TestStopCleanup:
    def test_stop_stops_every_watcher(self, db_path, tmp_path):
        p1 = tmp_path / "p1"
        p1.mkdir()
        p2 = tmp_path / "p2"
        p2.mkdir()
        _record_opt_in(db_path, p1)
        _record_opt_in(db_path, p2)

        svc = IngestionService(
            db_path, watcher_factory=_mock_watcher_factory,
            sweep_factory=_stub_sweep_factory,
        )
        svc.start()
        watchers = [r.watcher for r in svc.projects()]
        svc.stop()
        for w in watchers:
            assert w.stopped is True

    def test_stop_is_idempotent(self, db_path):
        svc = IngestionService(db_path, watcher_factory=_mock_watcher_factory)
        svc.start()
        svc.stop()
        svc.stop()      # must not raise / must not double-join

    def test_stop_joins_sweep_thread_within_timeout(self, db_path, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        _record_opt_in(db_path, proj)
        svc = IngestionService(
            db_path, watcher_factory=_mock_watcher_factory,
            sweep_factory=_stub_sweep_factory,
        )
        svc.start()
        sweep_thread = svc.get_project(proj).sweep_thread
        # Give the (very small) stub sweep a moment to settle naturally.
        if sweep_thread is not None:
            sweep_thread.join(timeout=2.0)
        svc.stop()
        if sweep_thread is not None:
            assert not sweep_thread.is_alive()


# ── End-to-end: real sweep against a real workspace ─────────────────────

class TestRealSweepIntegration:
    def test_start_runs_real_branch_switch_sweep(self, db_path, tmp_path):
        """Plug in the real sweep_factory (not the stub) and confirm
        the start() pipeline schedules + completes a sweep that adds
        chunks for a new file."""
        proj = tmp_path / "proj"
        proj.mkdir()
        _seed_python_file(proj, "a.py", num_funcs=2)
        _record_opt_in(db_path, proj)

        svc = IngestionService(
            db_path,
            watcher_factory=_mock_watcher_factory,
            # Real sweep — IngestionService passes embedder + skip_policy
            # for us so the workspace actually gets ingested.
            embedder=NoOpEmbedder(),
        )
        svc.start()
        runtime = svc.get_project(proj)
        # Give the background sweep up to 5 s to finish.
        if runtime.sweep_thread is not None:
            runtime.sweep_thread.join(timeout=5.0)
        svc.stop()

        assert runtime.sweep_result is not None
        # First sweep on a fresh project: file is "new", and the
        # reindex_file path produced at least one chunk.
        assert runtime.sweep_result.new_files == 1
        assert runtime.sweep_result.chunks_added > 0

        # And the chunks landed in the DB.
        c = sqlite3.connect(str(db_path))
        try:
            count = c.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_path LIKE ?",
                (f"%{proj.name}%",),
            ).fetchone()[0]
        finally:
            c.close()
        assert count > 0
