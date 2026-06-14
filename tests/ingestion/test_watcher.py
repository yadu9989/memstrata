"""Tests for V5.2-A Phase 35.7 — CodebaseWatcher + reindex_file."""
from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest

from memstrata.layer3._db import init_db

# Whole file requires sqlite-vec — the watcher mutates code_chunks_vec.
pytestmark = pytest.mark.requires_sqlite_vec
from memstrata.layer3.ingestion import (
    BackfillOrchestrator,
    NoOpEmbedder,
)
from memstrata.layer3.ingestion.orchestrator import record_opt_in
from memstrata.layer3.ingestion.watcher import (
    DEBOUNCE_SECONDS,
    CodebaseWatcher,
    NotOptedIn,
    ReindexResult,
    reindex_file,
)

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
    """Write a >50-line Python file to *root/name* so the chunker
    exercises the AST path instead of the file-fallback path."""
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


# ── Hard Rule 73: opt-in required ────────────────────────────────────────

class TestOptInGate:
    def test_constructor_refuses_without_opt_in(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        with pytest.raises(NotOptedIn, match="Hard Rule 73"):
            CodebaseWatcher(conn, project_id="p1", project_root=proj)

    def test_pending_state_refuses(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        record_opt_in(conn, proj, state="pending")
        with pytest.raises(NotOptedIn):
            CodebaseWatcher(conn, project_id="p1", project_root=proj)

    def test_opted_out_refuses(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        record_opt_in(conn, proj, state="opted_out")
        with pytest.raises(NotOptedIn):
            CodebaseWatcher(conn, project_id="p1", project_root=proj)

    def test_opted_in_allows(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        record_opt_in(conn, proj)
        # Should not raise.
        w = CodebaseWatcher(conn, project_id="p1", project_root=proj)
        assert w.project_id == "p1"


# ── Debounce semantics (§5.2 + Hard Rule 73 framing) ────────────────────

class TestDebounce:
    def test_drain_skips_paths_within_debounce_window(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        record_opt_in(conn, proj)
        _seed_python_file(proj, "a.py")

        clock_state = {"now": 1000.0}
        w = CodebaseWatcher(
            conn, project_id="p1", project_root=proj,
            clock=lambda: clock_state["now"],
        )

        w.feed_event(str(proj / "a.py"))
        # Drain immediately — must NOT process yet (no time has passed).
        results = w.drain_pending()
        assert results == []
        assert w.pending_count == 1

        # Advance time past the debounce window; now it processes.
        clock_state["now"] += DEBOUNCE_SECONDS + 0.05
        results = w.drain_pending()
        assert len(results) == 1
        assert w.pending_count == 0

    def test_rapid_repeated_events_collapse_to_one_drain(self, conn, tmp_path):
        """A flurry of saves on one file in <500ms should produce ONE
        reindex pass, not N. The pending map keys on path so re-enqueue
        just overwrites the last_seen timestamp."""
        proj = _seed_project(tmp_path)
        record_opt_in(conn, proj)
        path = _seed_python_file(proj, "b.py")

        clock_state = {"now": 1000.0}
        w = CodebaseWatcher(
            conn, project_id="p1", project_root=proj,
            clock=lambda: clock_state["now"],
        )

        # 10 rapid saves within 100ms.
        for tick in range(10):
            clock_state["now"] += 0.01
            w.feed_event(str(path))

        # Just inside the debounce window -> nothing settled yet.
        clock_state["now"] += DEBOUNCE_SECONDS - 0.05
        assert w.drain_pending() == []
        assert w.pending_count == 1

        # Now past the window.
        clock_state["now"] += 0.1
        results = w.drain_pending()
        assert len(results) == 1
        assert w.processed_count == 1

    def test_different_files_drain_independently(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        record_opt_in(conn, proj)
        a = _seed_python_file(proj, "a.py")
        b = _seed_python_file(proj, "b.py")

        clock_state = {"now": 1000.0}
        w = CodebaseWatcher(
            conn, project_id="p1", project_root=proj,
            clock=lambda: clock_state["now"],
        )
        w.feed_event(str(a))
        clock_state["now"] += 0.3
        w.feed_event(str(b))

        # Advance just past a's window but inside b's.
        clock_state["now"] += DEBOUNCE_SECONDS - 0.25
        results = w.drain_pending()
        assert len(results) == 1
        assert results[0].file_path.endswith("a.py")
        assert w.pending_count == 1

        clock_state["now"] += 0.5
        results2 = w.drain_pending()
        assert len(results2) == 1
        assert results2[0].file_path.endswith("b.py")


# ── Denylist filter (§35.5 -> watcher) ──────────────────────────────────

class TestDenylistFilter:
    def test_skips_node_modules_paths(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        record_opt_in(conn, proj)
        nm = proj / "node_modules" / "lib.py"
        nm.parent.mkdir(parents=True)
        nm.write_text("def x(): pass\n")

        w = CodebaseWatcher(conn, project_id="p1", project_root=proj)
        result = reindex_file(conn, "p1", proj, nm)
        assert result.added == 0
        assert "denylisted-dir" in (result.skipped_reason or "")
        # Nothing inserted.
        assert conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE file_path LIKE '%node_modules%'"
        ).fetchone()[0] == 0

    def test_skips_oversize_files(self, conn, tmp_path):
        from memstrata.layer3.ingestion.denylist import MAX_FILE_SIZE_BYTES
        proj = _seed_project(tmp_path)
        record_opt_in(conn, proj)
        big = proj / "big.py"
        big.write_bytes(b"x = 1\n" * ((MAX_FILE_SIZE_BYTES // 6) + 100))

        result = reindex_file(conn, "p1", proj, big)
        assert result.added == 0
        assert "too-large" in (result.skipped_reason or "")


# ── Diff-by-stable-hash (no-op when unchanged) ──────────────────────────

class TestIncrementalDiff:
    def _do_initial_backfill(self, conn, proj: Path) -> None:
        record_opt_in(conn, proj)
        orch = BackfillOrchestrator(
            conn, project_id="p1", project_path=proj,
            embedder=NoOpEmbedder(), respect_gitignore=False,
        )
        orch.run()

    def test_unchanged_file_does_no_work(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        f = _seed_python_file(proj, "x.py", num_funcs=3)
        self._do_initial_backfill(conn, proj)

        chunks_before = conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE project_id='p1' AND file_path=?",
            (str(f),),
        ).fetchone()[0]
        assert chunks_before >= 3

        result = reindex_file(conn, "p1", proj, f, embedder=NoOpEmbedder())
        assert result.added == 0
        assert result.removed == 0
        assert result.unchanged == chunks_before
        assert result.embedded == 0     # No new chunks -> no embedding work

    def test_edit_one_function_only_re_embeds_that_function(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        f = _seed_python_file(proj, "x.py", num_funcs=3)
        self._do_initial_backfill(conn, proj)

        # Modify fn_1's body. Other functions' stable_hash should
        # survive untouched.
        text = f.read_text(encoding="utf-8")
        text = text.replace(
            "def fn_1(a, b):\n    return a + b + 1",
            "def fn_1(a, b, c=10):\n    return a + b + c + 1",
        )
        f.write_text(text, encoding="utf-8")

        result = reindex_file(conn, "p1", proj, f, embedder=NoOpEmbedder())
        # The changed function shows up as one add + one remove.
        assert result.removed == 1
        assert result.added == 1
        assert result.embedded == 1

        # Total chunk count for this file is unchanged.
        chunks_after = conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE project_id='p1' AND file_path=?",
            (str(f),),
        ).fetchone()[0]
        assert chunks_after >= 3

    def test_adding_a_function_only_adds_one_chunk(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        f = _seed_python_file(proj, "x.py", num_funcs=2)
        self._do_initial_backfill(conn, proj)
        chunks_before = conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE project_id='p1' AND file_path=?",
            (str(f),),
        ).fetchone()[0]

        text = f.read_text(encoding="utf-8")
        text += "\n\ndef fn_new(x):\n    return x * 2\n"
        f.write_text(text, encoding="utf-8")

        result = reindex_file(conn, "p1", proj, f, embedder=NoOpEmbedder())
        assert result.added == 1
        assert result.removed == 0
        assert result.embedded == 1

        chunks_after = conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE project_id='p1' AND file_path=?",
            (str(f),),
        ).fetchone()[0]
        assert chunks_after == chunks_before + 1

    def test_removing_a_function_deletes_chunks_and_embeddings(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        f = _seed_python_file(proj, "x.py", num_funcs=4)
        self._do_initial_backfill(conn, proj)

        # Drop fn_3 entirely.
        text = f.read_text(encoding="utf-8")
        text = text.replace("def fn_3(a, b):\n    return a + b + 3\n", "")
        f.write_text(text, encoding="utf-8")

        # Snapshot embedding count before.
        vec_before = conn.execute("SELECT COUNT(*) FROM code_chunks_vec").fetchone()[0]

        result = reindex_file(conn, "p1", proj, f, embedder=NoOpEmbedder())
        assert result.removed >= 1
        # vec0 row also dropped, not orphaned.
        assert result.deleted_from_vec == result.removed

        vec_after = conn.execute("SELECT COUNT(*) FROM code_chunks_vec").fetchone()[0]
        assert vec_after == vec_before - result.deleted_from_vec

    def test_deleted_file_purges_all_chunks(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        f = _seed_python_file(proj, "x.py", num_funcs=3)
        self._do_initial_backfill(conn, proj)

        chunks_before = conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE project_id='p1' AND file_path=?",
            (str(f),),
        ).fetchone()[0]
        assert chunks_before > 0

        f.unlink()
        result = reindex_file(conn, "p1", proj, f, embedder=NoOpEmbedder())
        assert result.file_missing
        assert result.removed == chunks_before

        chunks_after = conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE project_id='p1' AND file_path=?",
            (str(f),),
        ).fetchone()[0]
        assert chunks_after == 0
        # file_hashes row also gone.
        hashes_after = conn.execute(
            "SELECT COUNT(*) FROM file_hashes WHERE project_id='p1' AND file_path=?",
            (str(f),),
        ).fetchone()[0]
        assert hashes_after == 0


# ── Watcher lifecycle ──────────────────────────────────────────────────

class TestLifecycle:
    def test_stop_is_idempotent(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        record_opt_in(conn, proj)
        w = CodebaseWatcher(conn, project_id="p1", project_root=proj)
        # Never started -> stop() should be a no-op rather than crash.
        w.stop()
        w.stop()       # second call must also be safe

    def test_pending_count_zero_when_idle(self, conn, tmp_path):
        proj = _seed_project(tmp_path)
        record_opt_in(conn, proj)
        w = CodebaseWatcher(conn, project_id="p1", project_root=proj)
        assert w.pending_count == 0
