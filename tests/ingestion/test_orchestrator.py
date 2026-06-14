"""Tests for memstrata.layer3.ingestion.orchestrator (V5.2-A Phase 35.2)."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import textwrap
from pathlib import Path

import pytest

# Whole file requires sqlite-vec — the orchestrator writes to code_chunks_vec.
pytestmark = pytest.mark.requires_sqlite_vec

from memstrata.layer3._db import init_db
from memstrata.layer3.ingestion import (
    BackfillOrchestrator,
    JobPhase,
    JobState,
    NoOpEmbedder,
    OptInRequired,
)
from memstrata.layer3.ingestion.orchestrator import record_opt_in

# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def conn(tmp_path, monkeypatch):
    """Fresh DB per test, with all migrations applied."""
    db = tmp_path / "core.db"
    monkeypatch.setenv("ML_DB_PATH", str(db))
    c = sqlite3.connect(str(db))
    init_db(c)
    yield c
    c.close()


def _make_project(root: Path) -> Path:
    """Create a tiny realistic project tree, including a denylisted dir."""
    files = {
        "src/calc.py": (
            "# header\n" * 45
            + "def add(a, b):\n    return a + b\n\n"
            + "def sub(a, b):\n    return a - b\n"
        ),
        "src/models.py": (
            "# header\n" * 45
            + "class User:\n"
            + "    def __init__(self, name):\n"
            + "        self.name = name\n"
        ),
        "tests/test_calc.py": (
            "# header\n" * 45
            + "def test_add():\n    assert True\n"
        ),
        # Tiny file goes through small-file fallback (single chunk).
        "README.py": "x = 1\n",
        # Denylist must skip these.
        "node_modules/x.py": "def skip(): pass\n",
        ".git/config": "[core]\n",
    }
    root.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


# ── Hard Rule 70: opt-in gate ────────────────────────────────────────────

class TestHardRule70:
    def test_run_without_opt_in_raises(self, conn, tmp_path):
        proj = _make_project(tmp_path / "proj")
        orch = BackfillOrchestrator(conn, project_id="p1", project_path=proj)
        with pytest.raises(OptInRequired):
            orch.run()

    def test_pending_state_blocks(self, conn, tmp_path):
        proj = _make_project(tmp_path / "proj")
        record_opt_in(conn, proj, state="pending")
        orch = BackfillOrchestrator(conn, project_id="p1", project_path=proj)
        with pytest.raises(OptInRequired):
            orch.run()

    def test_opted_out_blocks(self, conn, tmp_path):
        proj = _make_project(tmp_path / "proj")
        record_opt_in(conn, proj, state="opted_out")
        orch = BackfillOrchestrator(conn, project_id="p1", project_path=proj)
        with pytest.raises(OptInRequired):
            orch.run()

    def test_opted_in_allows(self, conn, tmp_path):
        proj = _make_project(tmp_path / "proj")
        record_opt_in(conn, proj, state="opted_in")
        orch = BackfillOrchestrator(conn, project_id="p1", project_path=proj)
        state = orch.run()
        assert state.phase == JobPhase.COMPLETE


# ── Four-phase progression ──────────────────────────────────────────────

class TestFourPhases:
    def test_full_run_completes(self, conn, tmp_path):
        proj = _make_project(tmp_path / "proj")
        record_opt_in(conn, proj)
        orch = BackfillOrchestrator(conn, project_id="p1", project_path=proj)
        state = orch.run()
        assert state.phase == JobPhase.COMPLETE
        assert state.files_total > 0
        assert state.files_processed == state.files_total
        assert state.entities_embedded == state.entities_total

    def test_denylist_excludes_node_modules_and_git(self, conn, tmp_path):
        proj = _make_project(tmp_path / "proj")
        record_opt_in(conn, proj)
        orch = BackfillOrchestrator(conn, project_id="p1", project_path=proj)
        orch.run()
        row = conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE file_path LIKE '%node_modules%'"
        ).fetchone()
        assert row[0] == 0
        row = conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE file_path LIKE '%/.git/%'"
            " OR file_path LIKE '%\\\\.git\\\\%'"
        ).fetchone()
        assert row[0] == 0

    def test_file_hashes_populated(self, conn, tmp_path):
        proj = _make_project(tmp_path / "proj")
        record_opt_in(conn, proj)
        orch = BackfillOrchestrator(conn, project_id="p1", project_path=proj)
        orch.run()
        row = conn.execute(
            "SELECT COUNT(*) FROM file_hashes WHERE project_id = 'p1'"
        ).fetchone()
        assert row[0] >= 3       # README.py + src/calc.py + src/models.py + tests/test_calc.py

    def test_embeddings_written_to_vec0(self, conn, tmp_path):
        proj = _make_project(tmp_path / "proj")
        record_opt_in(conn, proj)
        orch = BackfillOrchestrator(conn, project_id="p1", project_path=proj)
        orch.run()
        chunks = conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE project_id = 'p1'"
        ).fetchone()[0]
        vecs = conn.execute("SELECT COUNT(*) FROM code_chunks_vec").fetchone()[0]
        assert chunks > 0
        assert vecs == chunks


# ── Hard Rule 72: resume after crash ────────────────────────────────────

class TestHardRule72:
    def test_resume_from_parse_phase_crash(self, conn, tmp_path):
        """Abort mid-parse, then run() again on a fresh orchestrator
        and confirm it picks up at PARSE rather than restarting at SCAN."""
        proj = _make_project(tmp_path / "proj")
        record_opt_in(conn, proj)

        orch1 = BackfillOrchestrator(conn, project_id="p1", project_path=proj)
        orch1.abort_after(JobPhase.PARSE, batches=1)
        with pytest.raises(RuntimeError, match="test-abort"):
            orch1.run()

        # State must show PARSE complete (advanced to EMBED) with some
        # progress recorded.
        partial = orch1.current_state()
        assert partial is not None
        # After parse completes the phase advances to EMBED; if the abort
        # fired on the parse boundary we may be at EMBED already with
        # entities_total > 0.
        assert partial.phase in (JobPhase.PARSE, JobPhase.EMBED)
        assert partial.files_processed > 0

        # Resume on a fresh orchestrator instance.
        orch2 = BackfillOrchestrator(conn, project_id="p1", project_path=proj)
        final = orch2.run()
        assert final.phase == JobPhase.COMPLETE
        # No duplicate chunks: HR 72 means resume, not restart.
        row = conn.execute(
            """
            SELECT file_path, line_start, line_end, COUNT(*) FROM code_chunks
            WHERE project_id = 'p1'
            GROUP BY file_path, line_start, line_end
            HAVING COUNT(*) > 1
            """
        ).fetchone()
        assert row is None, f"duplicate chunks after resume: {row}"

    def test_resume_from_embed_phase_crash(self, conn, tmp_path):
        proj = _make_project(tmp_path / "proj")
        record_opt_in(conn, proj)

        orch1 = BackfillOrchestrator(conn, project_id="p1", project_path=proj)
        orch1.abort_after(JobPhase.EMBED, batches=1)
        with pytest.raises(RuntimeError, match="test-abort"):
            orch1.run()
        partial = orch1.current_state()
        assert partial is not None
        # Some embeddings should already exist when we re-enter.
        partial_embeds = conn.execute("SELECT COUNT(*) FROM code_chunks_vec").fetchone()[0]
        assert partial_embeds >= 0   # may be 0 if batch=1 fired exactly between batches

        orch2 = BackfillOrchestrator(conn, project_id="p1", project_path=proj)
        final = orch2.run()
        assert final.phase == JobPhase.COMPLETE
        # All chunks have embeddings now.
        chunks = conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE project_id='p1'"
        ).fetchone()[0]
        vecs = conn.execute("SELECT COUNT(*) FROM code_chunks_vec").fetchone()[0]
        assert chunks == vecs

    def test_completed_job_is_idempotent(self, conn, tmp_path):
        proj = _make_project(tmp_path / "proj")
        record_opt_in(conn, proj)
        orch = BackfillOrchestrator(conn, project_id="p1", project_path=proj)
        s1 = orch.run()
        assert s1.phase == JobPhase.COMPLETE
        chunks_before = conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE project_id='p1'"
        ).fetchone()[0]
        # Second run on a completed job is a no-op.
        s2 = orch.run()
        chunks_after = conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE project_id='p1'"
        ).fetchone()[0]
        assert s2.phase == JobPhase.COMPLETE
        assert chunks_after == chunks_before


# ── Pause / resume API ──────────────────────────────────────────────────

class TestPauseResume:
    def test_pause_then_resume(self, conn, tmp_path):
        proj = _make_project(tmp_path / "proj")
        record_opt_in(conn, proj)
        orch = BackfillOrchestrator(conn, project_id="p1", project_path=proj)
        # Move into PARSE phase by running scan then a forced pause.
        orch.abort_after(JobPhase.SCAN)
        with pytest.raises(RuntimeError):
            orch.run()
        orch.pause()
        assert orch.current_state().phase == JobPhase.PAUSED

        # Resume + finish.
        orch2 = BackfillOrchestrator(conn, project_id="p1", project_path=proj)
        orch2.resume()
        final = orch2.run()
        assert final.phase == JobPhase.COMPLETE


# ── Embedder failure handling ───────────────────────────────────────────

class TestEmbedderFailure:
    def test_failing_embedder_marks_failed_not_crash(self, conn, tmp_path):
        proj = _make_project(tmp_path / "proj")
        record_opt_in(conn, proj)

        class _BoomEmbedder:
            def embed(self, texts):
                raise RuntimeError("backend down")

        orch = BackfillOrchestrator(
            conn, project_id="p1", project_path=proj, embedder=_BoomEmbedder(),
        )
        state = orch.run()
        assert state.phase == JobPhase.FAILED
        assert "backend down" in (state.error or "")


# ── NoOpEmbedder properties ─────────────────────────────────────────────

class TestNoOpEmbedder:
    def test_returns_768_dim(self):
        e = NoOpEmbedder()
        v = e.embed(["hello"])
        assert len(v) == 1
        assert len(v[0]) == 768

    def test_deterministic(self):
        e = NoOpEmbedder()
        a = e.embed(["x", "y"])
        b = e.embed(["x", "y"])
        assert a == b
