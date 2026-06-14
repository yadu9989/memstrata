"""Phase 36 - Codebase ingest + /context/injection block tests.

Coverage:
  - Ingest walks a tmp project, skips dirs we don't care about, and stores
    rows in codebase_files + codebase_chunks.
  - Re-ingesting unchanged files is a no-op (SHA-based dedup).
  - Re-ingesting changed files replaces old chunks.
  - /context/injection returns the V5.1 empty-stub when nothing is ingested.
  - /context/injection returns a real block_text + stable hash + non-zero
    raw_codebase_tokens after ingestion.
  - The block_hash is stable across calls (cache-friendly per Hard Rule 50).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Whole file requires sqlite-vec — every test here exercises code_chunks_vec.
pytestmark = pytest.mark.requires_sqlite_vec

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ML_DB_PATH", str(tmp_path / "test_core.db"))


@pytest.fixture
def client(isolated_db):
    from memstrata.layer3.api_server import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_conn(tmp_path, isolated_db):
    """Direct connection to the test DB.

    Depends on ``isolated_db`` explicitly so this connection opens
    AFTER the env var is set and any lifespan-managed connection has
    released its WAL+SHM files (Windows file-locking edge case). Loads
    sqlite-vec on the connection so verification queries against vec0
    virtual tables (``code_chunks_vec`` etc.) succeed. Wraps the yield
    in try/finally so a test-side exception still releases the file
    lock — that's the failure mode that surfaces as a Windows access
    violation when tmp_path tears down.
    """
    path = tmp_path / "test_core.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    from memstrata.layer3._db import _load_vec_extension
    _load_vec_extension(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def sample_repo(tmp_path):
    """Build a tiny repo on disk: 1 README, 1 .py, 1 file in a skip-dir."""
    root = tmp_path / "myrepo"
    root.mkdir()
    (root / "README.md").write_text(
        "# Sample Project\n\nThis is the README for the test repo.\n" * 5,
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "def hello():\n    return 'world'\n\n" * 10,
        encoding="utf-8",
    )
    skip = root / "node_modules"
    skip.mkdir()
    (skip / "ignored.js").write_text("console.log('never indexed')", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Walker + chunker
# ---------------------------------------------------------------------------

class TestWalker:
    def test_iter_skips_node_modules(self, sample_repo):
        from memstrata.cli.ingest import iter_source_files
        rels = sorted(r.rel for r in iter_source_files(sample_repo))
        assert "README.md" in rels
        assert "main.py" in rels
        assert all("node_modules" not in r for r in rels), rels

    def test_chunk_text_splits_on_whitespace(self):
        from memstrata.cli.ingest import CHUNK_CHARS, chunk_text
        text = ("hello world " * 1000).strip()
        chunks = chunk_text(text, chunk_chars=200)
        assert len(chunks) > 1
        # No chunk should mid-cut a word ("hello" never followed by a non-space)
        for c in chunks:
            assert not c.endswith("hell")

    def test_chunk_text_empty(self):
        from memstrata.cli.ingest import chunk_text
        assert chunk_text("") == []
        assert chunk_text("   \n\t  ") == []


# ---------------------------------------------------------------------------
# Ingest (DB side-effects)
# ---------------------------------------------------------------------------

class TestIngestProject:
    def test_first_run_indexes_files_without_embed(self, sample_repo, isolated_db):
        from memstrata.cli.ingest import ingest_project
        s = ingest_project(sample_repo, embed=False)
        assert s.files_indexed == 2
        assert s.files_unchanged == 0
        assert s.chunks_written >= 2
        assert s.chunks_embedded == 0
        assert s.tokens_total > 0

    def test_rerun_unchanged_is_noop(self, sample_repo, isolated_db):
        from memstrata.cli.ingest import ingest_project
        ingest_project(sample_repo, embed=False)
        s2 = ingest_project(sample_repo, embed=False)
        assert s2.files_indexed == 0
        assert s2.files_unchanged == 2
        assert s2.chunks_written == 0

    def test_changed_file_is_reindexed(self, sample_repo, isolated_db):
        from memstrata.cli.ingest import ingest_project
        ingest_project(sample_repo, embed=False)
        (sample_repo / "main.py").write_text(
            "def changed():\n    return 'updated'\n", encoding="utf-8"
        )
        s2 = ingest_project(sample_repo, embed=False)
        assert s2.files_indexed == 1
        assert s2.files_unchanged == 1

    def test_rows_land_in_correct_tables(self, sample_repo, isolated_db, tmp_path):
        from memstrata.cli.ingest import ingest_project
        ingest_project(sample_repo, embed=False, project_id="myproj")
        conn = sqlite3.connect(str(tmp_path / "test_core.db"))
        conn.row_factory = sqlite3.Row
        try:
            n_files = conn.execute(
                "SELECT COUNT(*) FROM codebase_files WHERE project_id='myproj'"
            ).fetchone()[0]
            n_chunks = conn.execute(
                "SELECT COUNT(*) FROM codebase_chunks WHERE project_id='myproj'"
            ).fetchone()[0]
            assert n_files == 2
            assert n_chunks >= 2
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# /context/injection endpoint
# ---------------------------------------------------------------------------

class TestContextInjectionEndpoint:
    def test_returns_empty_stub_when_no_ingest(self, client):
        r = client.get("/context/injection", params={"project_id": "never-ingested"})
        assert r.status_code == 200
        d = r.json()
        assert d["block_text"] == ""
        assert d["block_hash"] == "empty"
        assert d["token_count"] == 0
        assert d["raw_codebase_tokens"] is None

    def test_returns_real_block_after_ingest(self, sample_repo, client):
        from memstrata.cli.ingest import ingest_project
        ingest_project(sample_repo, embed=False, project_id="myproj")

        r = client.get("/context/injection", params={"project_id": "myproj"})
        assert r.status_code == 200
        d = r.json()
        assert d["block_text"] != ""
        assert d["block_hash"] != "empty"
        assert d["token_count"] > 0
        assert d["raw_codebase_tokens"] is not None
        assert d["raw_codebase_tokens"] > 0
        # README should appear in the block (docs prioritized).
        assert "README.md" in d["block_text"]

    def test_block_hash_is_stable(self, sample_repo, client):
        """Hard Rule 50: same on-disk content -> same hash, so the harness's
        FRESH_FULL / SKIP / APPEND_DELTA path works (prefix-cache stays warm)."""
        from memstrata.cli.ingest import ingest_project
        ingest_project(sample_repo, embed=False, project_id="stable")

        h1 = client.get("/context/injection", params={"project_id": "stable"}).json()["block_hash"]
        h2 = client.get("/context/injection", params={"project_id": "stable"}).json()["block_hash"]
        assert h1 == h2

    def test_block_hash_changes_when_content_changes(self, sample_repo, client):
        from memstrata.cli.ingest import ingest_project
        ingest_project(sample_repo, embed=False, project_id="moving")
        h1 = client.get("/context/injection", params={"project_id": "moving"}).json()["block_hash"]

        # Mutate the file then re-ingest.
        (sample_repo / "README.md").write_text("# Totally different content\n", encoding="utf-8")
        ingest_project(sample_repo, embed=False, project_id="moving")
        h2 = client.get("/context/injection", params={"project_id": "moving"}).json()["block_hash"]

        assert h1 != h2

    def test_legacy_default_project_still_returns_stub(self, client):
        r = client.get("/context/injection")  # no params -> project_id="default"
        assert r.status_code == 200
        assert r.json()["block_hash"] == "empty"
