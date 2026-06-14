"""Tests for V5.2-A Phase 35.3 — progress snapshot + control surface."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memstrata.layer3._db import init_db
from memstrata.layer3.ingestion import (
    BackfillOrchestrator,
    JobPhase,
    NoOpEmbedder,
    ResourcePolicy,
)
from memstrata.layer3.ingestion.orchestrator import record_opt_in
from memstrata.layer3.ingestion.progress import (
    CONTROL_REGISTRY,
    ControlRegistry,
    ControlState,
    build_snapshot,
)
from memstrata.layer3.ingestion.resource_policy import BatteryState

# ── ETA / snapshot math ───────────────────────────────────────────────

class TestBuildSnapshot:
    def test_pct_fields_clamp_0_to_1(self):
        row = {
            "project_id": "p1", "project_path": "/p",
            "phase": "parse", "files_total": 10, "files_processed": 5,
            "entities_total": 0, "entities_embedded": 0,
            "last_processed_file": None, "started_at": "",
            "completed_at": None, "error": None,
        }
        snap = build_snapshot(row)
        assert snap.files_pct == 0.5
        # No entities counted yet -> 0
        assert snap.entities_pct == 0.0

    def test_zero_denominator_is_zero_pct(self):
        row = {
            "project_id": "p1", "project_path": "/p",
            "phase": "scan", "files_total": 0, "files_processed": 0,
            "entities_total": 0, "entities_embedded": 0,
            "last_processed_file": None, "started_at": "",
            "completed_at": None, "error": None,
        }
        assert build_snapshot(row).files_pct == 0.0

    def test_eta_computed_during_parse(self):
        # Simulate: started 10s ago, 5 of 10 files done, 4 of 10 entities done.
        started = (datetime.now(timezone.utc) - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
        row = {
            "project_id": "p1", "project_path": "/p", "phase": "parse",
            "files_total": 10, "files_processed": 5,
            "entities_total": 10, "entities_embedded": 4,
            "last_processed_file": "src/a.py", "started_at": started,
            "completed_at": None, "error": None,
        }
        snap = build_snapshot(row)
        # Rate ~= 0.5 files/s; remaining 5 -> ETA ~= 10s.
        # Allow slack for the wall clock between now() calls.
        assert snap.eta_seconds is not None
        assert 5 < snap.eta_seconds < 20

    def test_no_eta_in_scan_phase(self):
        row = {
            "project_id": "p1", "project_path": "/p",
            "phase": "scan", "files_total": 0, "files_processed": 0,
            "entities_total": 0, "entities_embedded": 0,
            "last_processed_file": None, "started_at": "",
            "completed_at": None, "error": None,
        }
        snap = build_snapshot(row)
        assert snap.eta_seconds is None

    def test_paused_phase_reflected_as_paused(self):
        row = {
            "project_id": "p1", "project_path": "/p",
            "phase": "paused", "files_total": 10, "files_processed": 5,
            "entities_total": 0, "entities_embedded": 0,
            "last_processed_file": None, "started_at": "",
            "completed_at": None, "error": None,
        }
        assert build_snapshot(row).is_paused

    def test_to_dict_keys_stable(self):
        row = {
            "project_id": "p1", "project_path": "/p",
            "phase": "scan", "files_total": 0, "files_processed": 0,
            "entities_total": 0, "entities_embedded": 0,
            "last_processed_file": None, "started_at": "",
            "completed_at": None, "error": None,
        }
        d = build_snapshot(row).to_dict()
        required = {
            "project_id", "project_path", "phase",
            "files_total", "files_processed",
            "entities_total", "entities_embedded",
            "files_pct", "entities_pct",
            "elapsed_seconds", "eta_seconds",
            "rate_files_per_second", "rate_entities_per_second",
            "is_paused", "is_cancelling", "soft_pause_reason",
        }
        assert required <= set(d.keys())


# ── ControlState plumbing ────────────────────────────────────────────

class TestControlRegistry:
    def test_get_or_create_returns_same_instance(self):
        reg = ControlRegistry()
        a = reg.get_or_create("p1")
        b = reg.get_or_create("p1")
        assert a is b

    def test_drop_removes_state(self):
        reg = ControlRegistry()
        reg.get_or_create("p1")
        assert reg.get("p1") is not None
        reg.drop("p1")
        assert reg.get("p1") is None


# ── Orchestrator <-> control flag integration ────────────────────────

class TestOrchestratorPauseFlag:
    def _seed(self, tmp_path, conn):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "src").mkdir()
        # Three files so embed phase has > 1 batch worth of work.
        for i in range(3):
            f = proj / "src" / f"m{i}.py"
            f.write_text(
                "# header\n" * 45
                + f"def f{i}():\n    return {i}\n"
                + "\n".join(f"def g{i}_{j}(): return {j}" for j in range(20))
                + "\n"
            )
        record_opt_in(conn, proj)
        return proj

    @pytest.fixture
    def conn(self, tmp_path, monkeypatch):
        db = tmp_path / "core.db"
        monkeypatch.setenv("ML_DB_PATH", str(db))
        c = sqlite3.connect(str(db))
        init_db(c)
        yield c
        c.close()

    def test_pause_flag_stops_at_phase_boundary(self, tmp_path, conn):
        proj = self._seed(tmp_path, conn)
        control = ControlState()
        control.pause_flag.set()
        orch = BackfillOrchestrator(
            conn, project_id="p1", project_path=proj,
            embedder=NoOpEmbedder(),
            control_state=control,
            respect_gitignore=False,
        )
        state = orch.run()
        # Pause was already set when embed phase entered -> phase=paused.
        assert state.phase == JobPhase.PAUSED

    def test_cancel_flag_stops_embed_phase(self, tmp_path, conn):
        proj = self._seed(tmp_path, conn)
        control = ControlState()
        # Start with no pause; set cancel just before the first embed batch.
        orch = BackfillOrchestrator(
            conn, project_id="p1", project_path=proj,
            embedder=NoOpEmbedder(),
            control_state=control,
            respect_gitignore=False,
        )
        # Run scan + parse normally, then trip cancel before embed runs.
        orch.abort_after(JobPhase.PARSE)
        with pytest.raises(RuntimeError):
            orch.run()
        control.cancel_flag.set()
        orch2 = BackfillOrchestrator(
            conn, project_id="p1", project_path=proj,
            embedder=NoOpEmbedder(),
            control_state=control,
            respect_gitignore=False,
        )
        state = orch2.run()
        assert state.phase == JobPhase.PAUSED


# ── Resource policy soft pause ───────────────────────────────────────

class TestSoftPause:
    @pytest.fixture
    def conn(self, tmp_path, monkeypatch):
        db = tmp_path / "core.db"
        monkeypatch.setenv("ML_DB_PATH", str(db))
        c = sqlite3.connect(str(db))
        init_db(c)
        yield c
        c.close()

    def test_resource_policy_soft_pause_blocks_then_continues(self, tmp_path, conn):
        proj = tmp_path / "proj"
        proj.mkdir()
        f = proj / "x.py"
        f.write_text(
            "# header\n" * 45 + "def f(): return 1\n"
        )
        record_opt_in(conn, proj)

        # Policy that says "pause for first 2 calls, then OK".
        calls = {"n": 0}

        def battery():
            calls["n"] += 1
            if calls["n"] < 3:
                return BatteryState(on_battery_power=True)
            return BatteryState(on_battery_power=False)

        policy = ResourcePolicy(
            battery_detector=battery,
            typing_idle_detector=lambda: None,
        )
        sleeps: list[float] = []
        orch = BackfillOrchestrator(
            conn, project_id="p1", project_path=proj,
            embedder=NoOpEmbedder(),
            resource_policy=policy,
            control_state=ControlState(),
            sleep=lambda s: sleeps.append(s),
            respect_gitignore=False,
        )
        state = orch.run()
        # Despite the soft pauses, we still completed.
        assert state.phase == JobPhase.COMPLETE
        # And we slept while paused (orchestrator backed off).
        assert sleeps, "expected at least one soft-pause sleep"
        # Soft pause reason was cleared after AC came back.
        assert orch.control_state.soft_pause_reason is None
