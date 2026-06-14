"""Tests for V5.2-A Phase 35.8 — branch-switch / mass-mutation sweep."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memstrata.layer3._db import init_db
from memstrata.layer3.ingestion import (
    BackfillOrchestrator,
    NoOpEmbedder,
    SweepResult,
    sweep_branch_switch,
)
from memstrata.layer3.ingestion.orchestrator import record_opt_in

# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = tmp_path / "core.db"
    monkeypatch.setenv("ML_DB_PATH", str(db))
    c = sqlite3.connect(str(db))
    init_db(c)
    yield c
    c.close()


def _seed_python_file(root: Path, name: str, *, num_funcs: int = 3) -> Path:
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


def _seed_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    return proj


def _do_initial_backfill(conn: sqlite3.Connection, proj: Path) -> None:
    record_opt_in(conn, proj)
    orch = BackfillOrchestrator(
        conn, project_id="p1", project_path=proj,
        embedder=NoOpEmbedder(), respect_gitignore=False,
    )
    orch.run()


# ── No-change baseline ───────────────────────────────────────────────────

class TestNoChanges:
    def test_unchanged_workspace_no_op(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        _seed_python_file(proj, "a.py")
        _seed_python_file(proj, "b.py")
        _do_initial_backfill(conn, proj)

        chunks_before = conn.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0]

        result = sweep_branch_switch(conn, "p1", proj, embedder=NoOpEmbedder())
        assert isinstance(result, SweepResult)
        assert result.new_files == 0
        assert result.modified_files == 0
        assert result.deleted_files == 0
        assert result.unchanged_files == 2          # two files seeded
        assert result.chunks_added == 0
        assert result.chunks_removed == 0

        chunks_after = conn.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0]
        assert chunks_after == chunks_before


# ── New file (e.g. `git checkout` brought it in) ─────────────────────────

class TestNewFiles:
    def test_new_file_detected_and_chunked(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        _seed_python_file(proj, "old.py")
        _do_initial_backfill(conn, proj)
        chunks_before = conn.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0]

        # New file appears AFTER the backfill — simulates a branch switch.
        _seed_python_file(proj, "new.py", num_funcs=2)

        result = sweep_branch_switch(conn, "p1", proj, embedder=NoOpEmbedder())
        assert result.new_files == 1
        assert result.unchanged_files == 1
        assert result.chunks_added > 0

        chunks_after = conn.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0]
        assert chunks_after > chunks_before

        # file_hashes row for the new file now exists.
        new_file = str((proj / "new.py").resolve())
        row = conn.execute(
            "SELECT content_hash FROM file_hashes WHERE project_id='p1' AND file_path=?",
            (new_file,),
        ).fetchone()
        assert row is not None


# ── Modified file (the common case after `git checkout`) ─────────────────

class TestModifiedFiles:
    def test_modified_file_routed_through_reindex(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        f = _seed_python_file(proj, "x.py", num_funcs=3)
        _do_initial_backfill(conn, proj)

        # Edit fn_1's body; everything else stays the same.
        text = f.read_text(encoding="utf-8")
        text = text.replace(
            "def fn_1(a, b):\n    return a + b + 1",
            "def fn_1(a, b, c=10):\n    return a + b + c + 1",
        )
        f.write_text(text, encoding="utf-8")

        result = sweep_branch_switch(conn, "p1", proj, embedder=NoOpEmbedder())
        assert result.modified_files == 1
        assert result.unchanged_files == 0
        # The hash-diff inside reindex_file finds exactly one chunk add
        # and one chunk remove — the rest survive intact.
        assert result.chunks_added == 1
        assert result.chunks_removed == 1
        assert result.chunks_embedded == 1

    def test_modified_file_updates_file_hashes_row(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        f = _seed_python_file(proj, "y.py")
        _do_initial_backfill(conn, proj)
        old_hash = conn.execute(
            "SELECT content_hash FROM file_hashes WHERE project_id='p1' AND file_path=?",
            (str(f.resolve()),),
        ).fetchone()[0]

        f.write_text(f.read_text() + "\ndef extra(): pass\n", encoding="utf-8")
        sweep_branch_switch(conn, "p1", proj, embedder=NoOpEmbedder())

        new_hash = conn.execute(
            "SELECT content_hash FROM file_hashes WHERE project_id='p1' AND file_path=?",
            (str(f.resolve()),),
        ).fetchone()[0]
        assert new_hash != old_hash


# ── Deleted file (mass `git rm`, branch checkout that drops a file) ─────

class TestDeletedFiles:
    def test_deleted_file_purges_chunks_and_hashes(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        f = _seed_python_file(proj, "doomed.py", num_funcs=3)
        _do_initial_backfill(conn, proj)
        # We have chunks for this file at this point.
        before_chunks = conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE project_id='p1' AND file_path=?",
            (str(f.resolve()),),
        ).fetchone()[0]
        assert before_chunks > 0

        f.unlink()
        result = sweep_branch_switch(conn, "p1", proj, embedder=NoOpEmbedder())
        assert result.deleted_files == 1
        assert result.chunks_removed == before_chunks

        # Chunks AND file_hashes row both gone.
        after_chunks = conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE file_path=?",
            (str(f.resolve()),),
        ).fetchone()[0]
        after_hashes = conn.execute(
            "SELECT COUNT(*) FROM file_hashes WHERE file_path=?",
            (str(f.resolve()),),
        ).fetchone()[0]
        assert after_chunks == 0
        assert after_hashes == 0


# ── Mixed mass-mutation (the real branch-switch scenario) ───────────────

class TestMassMutation:
    def test_simultaneous_add_modify_delete(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        keep = _seed_python_file(proj, "keep.py")
        modify = _seed_python_file(proj, "modify.py", num_funcs=2)
        delete = _seed_python_file(proj, "delete.py", num_funcs=2)
        _do_initial_backfill(conn, proj)

        # Branch switch: drop delete.py, edit modify.py, add new.py.
        delete.unlink()
        text = modify.read_text(encoding="utf-8")
        modify.write_text(
            text.replace("def fn_0(a, b):", "def fn_0(a, b, extra=1):"),
            encoding="utf-8",
        )
        _seed_python_file(proj, "new.py", num_funcs=4)

        result = sweep_branch_switch(conn, "p1", proj, embedder=NoOpEmbedder())
        assert result.new_files == 1
        assert result.modified_files == 1
        assert result.deleted_files == 1
        assert result.unchanged_files == 1     # only keep.py survives untouched

    def test_empty_project_sweep_is_safe(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        # No backfill, no opt-in row needed — sweep only reads file_hashes.
        result = sweep_branch_switch(conn, "p1", proj, embedder=NoOpEmbedder())
        assert result.new_files == 0
        assert result.modified_files == 0
        assert result.deleted_files == 0
        assert result.unchanged_files == 0
