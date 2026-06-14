"""Tests for Phase 34.1 (schema migration) and 34.2 (embedding worker).

Verifies:
  - embedding_queue and telemetry_timeline_vec tables are created by init_db.
  - enqueue_for_embedding inserts into embedding_queue (INSERT OR IGNORE idempotent).
  - POST /telemetry/session enqueues the resulting timeline row.
  - Worker drains pending items when given a mock embedding function.
  - Worker marks empty-text rows completed without calling the embedding API.
  - Worker increments attempts and records last_error on Ollama failure.
  - Worker does NOT block after MAX_ATTEMPTS exceeded.
  - parse_recorded_at returns UTC-aware datetime for both space and T separators.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ML_DB_PATH", str(tmp_path / "test_phase34.db"))


@pytest.fixture
def db_conn(tmp_path, isolated_db):
    from memstrata.layer3._db import get_db_path, init_db
    path = get_db_path()
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def client(isolated_db):
    from memstrata.layer3.api_server import app
    with TestClient(app) as c:
        yield c


def _post_turn(client, *, session_id="s1", turn_id=1, project_id="proj",
               provider="anthropic", external_session_id="ext-abc",
               role="user", text="hello world from the test suite"):
    return client.post("/telemetry/session", json={
        "session_id": session_id,
        "turn_id": turn_id,
        "project_id": project_id,
        "provider": provider,
        "external_session_id": external_session_id,
        "role": role,
        "text": text,
    })


# ---------------------------------------------------------------------------
# 34.1 — Schema migration
# ---------------------------------------------------------------------------

class TestSchemaMigration:
    def test_embedding_queue_table_exists(self, db_conn):
        row = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='embedding_queue'"
        ).fetchone()
        assert row is not None, "embedding_queue table was not created"

    def test_telemetry_timeline_vec_table_exists(self, db_conn):
        row = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE name='telemetry_timeline_vec'"
        ).fetchone()
        assert row is not None, "telemetry_timeline_vec virtual table was not created"

    def test_embedding_queue_columns(self, db_conn):
        cols = {r[1] for r in db_conn.execute("PRAGMA table_info(embedding_queue)")}
        assert {"id", "timeline_id", "enqueued_at", "attempts", "last_error", "completed_at"} <= cols

    def test_embedding_queue_unique_on_timeline_id(self, db_conn):
        db_conn.execute(
            "INSERT INTO telemetry_session_timeline (session_id, turn_id, project_id) VALUES ('s', 1, 'p')"
        )
        db_conn.commit()
        row = db_conn.execute("SELECT id FROM telemetry_session_timeline").fetchone()
        tid = row[0]

        db_conn.execute("INSERT OR IGNORE INTO embedding_queue (timeline_id) VALUES (?)", (tid,))
        db_conn.execute("INSERT OR IGNORE INTO embedding_queue (timeline_id) VALUES (?)", (tid,))
        db_conn.commit()
        count = db_conn.execute(
            "SELECT COUNT(*) FROM embedding_queue WHERE timeline_id = ?", (tid,)
        ).fetchone()[0]
        assert count == 1, "UNIQUE constraint on timeline_id should prevent duplicates"

    def test_backfill_enqueues_existing_rows(self, db_conn):
        """Rows present in the timeline must appear in embedding_queue after init_db."""
        from memstrata.layer3._db import get_db_path
        # Insert two timeline rows via the already-initialised db_conn fixture.
        for turn in (10, 11):
            db_conn.execute(
                "INSERT INTO telemetry_session_timeline (session_id, turn_id, project_id) "
                "VALUES ('s_backfill', ?, 'p')",
                (turn,),
            )
        db_conn.commit()

        # Wipe the queue and re-run only the Phase 34 migration to simulate
        # what happens when migration 012 runs against an existing populated DB.
        db_conn.execute("DELETE FROM embedding_queue")
        db_conn.commit()
        from memstrata.layer3._db import _migrate_phase_34
        _migrate_phase_34(db_conn)

        count = db_conn.execute("SELECT COUNT(*) FROM embedding_queue").fetchone()[0]
        assert count >= 2, "Backfill should have enqueued the pre-existing timeline rows"


# ---------------------------------------------------------------------------
# 34.1 — enqueue_for_embedding helper
# ---------------------------------------------------------------------------

class TestEnqueueHelper:
    def test_enqueue_inserts_row(self, db_conn):
        from memstrata.layer3._db import enqueue_for_embedding
        db_conn.execute(
            "INSERT INTO telemetry_session_timeline (session_id, turn_id, project_id) VALUES ('s', 1, 'p')"
        )
        db_conn.commit()
        tid = db_conn.execute("SELECT id FROM telemetry_session_timeline").fetchone()[0]

        db_conn.execute("DELETE FROM embedding_queue WHERE timeline_id = ?", (tid,))
        db_conn.commit()

        enqueue_for_embedding(db_conn, tid)
        row = db_conn.execute(
            "SELECT timeline_id, attempts, completed_at FROM embedding_queue WHERE timeline_id = ?",
            (tid,),
        ).fetchone()
        assert row is not None
        assert row[0] == tid
        assert row[1] == 0
        assert row[2] is None

    def test_enqueue_is_idempotent(self, db_conn):
        from memstrata.layer3._db import enqueue_for_embedding
        db_conn.execute(
            "INSERT INTO telemetry_session_timeline (session_id, turn_id, project_id) VALUES ('s', 2, 'p')"
        )
        db_conn.commit()
        tid = db_conn.execute(
            "SELECT id FROM telemetry_session_timeline WHERE turn_id=2"
        ).fetchone()[0]

        enqueue_for_embedding(db_conn, tid)
        enqueue_for_embedding(db_conn, tid)  # second call must not raise or duplicate
        count = db_conn.execute(
            "SELECT COUNT(*) FROM embedding_queue WHERE timeline_id=?", (tid,)
        ).fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# 34.1 — POST /telemetry/session enqueues the turn
# ---------------------------------------------------------------------------

class TestIngestEnqueue:
    def test_post_turn_enqueues_timeline_id(self, client):
        from memstrata.layer3._db import get_db_path
        resp = _post_turn(client)
        assert resp.status_code == 200

        conn = sqlite3.connect(str(get_db_path()))
        tst = conn.execute(
            "SELECT id FROM telemetry_session_timeline ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert tst is not None, "No telemetry row inserted"
        tid = tst[0]

        eq = conn.execute(
            "SELECT timeline_id FROM embedding_queue WHERE timeline_id = ?", (tid,)
        ).fetchone()
        conn.close()
        assert eq is not None, f"timeline_id {tid} was not enqueued after POST /telemetry/session"

    def test_upsert_turn_does_not_duplicate_queue_entry(self, client):
        from memstrata.layer3._db import get_db_path
        # Two POSTs with same message_id → UPSERT → one timeline row, one queue entry
        payload = {
            "session_id": "s_upsert",
            "turn_id": 1,
            "project_id": "proj",
            "provider": "openai",
            "external_session_id": "ext-upsert",
            "message_id": "msg-abc",
            "role": "user",
            "text": "first version",
        }
        client.post("/telemetry/session", json=payload)
        payload["text"] = "updated version"
        client.post("/telemetry/session", json=payload)

        conn = sqlite3.connect(str(get_db_path()))
        tst_count = conn.execute(
            "SELECT COUNT(*) FROM telemetry_session_timeline WHERE session_id='s_upsert'"
        ).fetchone()[0]
        tst_row = conn.execute(
            "SELECT id FROM telemetry_session_timeline WHERE session_id='s_upsert'"
        ).fetchone()
        eq_count = conn.execute(
            "SELECT COUNT(*) FROM embedding_queue WHERE timeline_id=?", (tst_row[0],)
        ).fetchone()[0]
        conn.close()

        assert tst_count == 1, "UPSERT should produce one telemetry row"
        assert eq_count == 1, "UPSERT should produce one embedding_queue entry"


# ---------------------------------------------------------------------------
# 34.2 — EmbeddingWorker behaviour
# ---------------------------------------------------------------------------

def _make_fake_embedding(dim: int = 768) -> list[float]:
    return [0.1] * dim


class TestEmbeddingWorker:
    """Worker tests use the isolated_db fixture (via autouse) for DB isolation."""

    def _setup_timeline_row(self, db_conn, text="a real sentence worth embedding"):
        db_conn.execute(
            "INSERT INTO telemetry_session_timeline (session_id, turn_id, project_id, text) "
            "VALUES ('s', 1, 'p', ?)",
            (text,),
        )
        db_conn.commit()
        tid = db_conn.execute(
            "SELECT id FROM telemetry_session_timeline ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        db_conn.execute("DELETE FROM embedding_queue WHERE timeline_id = ?", (tid,))
        db_conn.execute("INSERT INTO embedding_queue (timeline_id) VALUES (?)", (tid,))
        db_conn.commit()
        return tid

    def test_worker_drains_queue_with_mock_embedding(self, db_conn):
        from memstrata.layer3._db import get_db_path
        from memstrata.workers.embedding_worker import EmbeddingWorker

        tid = self._setup_timeline_row(db_conn)

        worker = EmbeddingWorker()
        with patch.object(
            worker, "_embed_batch",
            return_value=[[_make_fake_embedding()]],
        ):
            # _embed_batch returns list of embeddings; fix the return to proper shape
            worker._embed_batch = lambda texts: [_make_fake_embedding() for _ in texts]
            worker.start()
            # Give the worker time to drain the single item
            deadline = time.time() + 5.0
            while time.time() < deadline:
                conn = sqlite3.connect(str(get_db_path()))
                row = conn.execute(
                    "SELECT completed_at FROM embedding_queue WHERE timeline_id=?", (tid,)
                ).fetchone()
                conn.close()
                if row and row[0]:
                    break
                time.sleep(0.1)
            worker.stop()

        conn = sqlite3.connect(str(get_db_path()))
        eq_row = conn.execute(
            "SELECT completed_at FROM embedding_queue WHERE timeline_id=?", (tid,)
        ).fetchone()
        assert eq_row and eq_row[0] is not None, "Worker should have marked the item completed"

    def test_worker_skips_empty_text_rows(self, db_conn):
        from memstrata.layer3._db import get_db_path
        from memstrata.workers.embedding_worker import EmbeddingWorker

        # Insert a row with empty text
        db_conn.execute(
            "INSERT INTO telemetry_session_timeline (session_id, turn_id, project_id, text) "
            "VALUES ('s_empty', 99, 'p', '')"
        )
        db_conn.commit()
        tid = db_conn.execute(
            "SELECT id FROM telemetry_session_timeline WHERE turn_id=99"
        ).fetchone()[0]
        db_conn.execute("DELETE FROM embedding_queue WHERE timeline_id=?", (tid,))
        db_conn.execute("INSERT INTO embedding_queue (timeline_id) VALUES (?)", (tid,))
        db_conn.commit()

        worker = EmbeddingWorker()
        embed_called = []
        worker._embed_batch = lambda texts: (embed_called.append(texts), [])[1]
        worker.start()

        deadline = time.time() + 5.0
        while time.time() < deadline:
            conn = sqlite3.connect(str(get_db_path()))
            row = conn.execute(
                "SELECT completed_at FROM embedding_queue WHERE timeline_id=?", (tid,)
            ).fetchone()
            conn.close()
            if row and row[0]:
                break
            time.sleep(0.1)
        worker.stop()

        assert len(embed_called) == 0, "Empty-text rows must not reach the embedding API"

        conn = sqlite3.connect(str(get_db_path()))
        eq_row = conn.execute(
            "SELECT completed_at FROM embedding_queue WHERE timeline_id=?", (tid,)
        ).fetchone()
        conn.close()
        assert eq_row and eq_row[0], "Empty-text rows should be marked completed (skipped)"

    def test_worker_increments_attempts_on_ollama_failure(self, db_conn):
        from memstrata.layer3._db import get_db_path
        from memstrata.workers.embedding_worker import EmbeddingWorker

        tid = self._setup_timeline_row(db_conn, text="this text will fail to embed")

        worker = EmbeddingWorker()
        worker._embed_batch = lambda texts: None  # simulate Ollama unavailable

        # Run one poll cycle manually (don't start thread to keep test deterministic)
        conn = worker._open_conn()
        batch = worker._get_pending(conn)
        assert any(item.timeline_id == tid for item in batch)
        worker._process_batch(conn, batch)
        conn.close()

        conn = sqlite3.connect(str(get_db_path()))
        eq_row = conn.execute(
            "SELECT attempts, last_error, completed_at FROM embedding_queue WHERE timeline_id=?",
            (tid,),
        ).fetchone()
        conn.close()
        assert eq_row[0] == 1, "Attempts should be incremented after Ollama failure"
        assert eq_row[1] == "ollama_unavailable"
        assert eq_row[2] is None, "Item should NOT be marked completed on failure"

    def test_worker_stops_retrying_after_max_attempts(self, db_conn):
        from memstrata.layer3._db import get_db_path
        from memstrata.workers.embedding_worker import EmbeddingWorker

        tid = self._setup_timeline_row(db_conn, text="exhausted item")
        # Set attempts to MAX_ATTEMPTS so it's already at the limit
        db_conn.execute(
            "UPDATE embedding_queue SET attempts=? WHERE timeline_id=?",
            (EmbeddingWorker.MAX_ATTEMPTS, tid),
        )
        db_conn.commit()

        worker = EmbeddingWorker()
        conn = worker._open_conn()
        batch = worker._get_pending(conn)
        conn.close()

        assert not any(item.timeline_id == tid for item in batch), (
            "Items at MAX_ATTEMPTS should not be picked up by the worker"
        )


# ---------------------------------------------------------------------------
# parse_recorded_at utility
# ---------------------------------------------------------------------------

class TestParseRecordedAt:
    def test_space_separator_returns_utc(self):
        from memstrata.layer3._db import parse_recorded_at
        dt = parse_recorded_at("2026-06-05 02:42:23")
        assert dt.tzinfo is not None
        assert dt.tzinfo == timezone.utc
        assert dt.year == 2026 and dt.month == 6 and dt.day == 5

    def test_t_separator_returns_utc(self):
        from memstrata.layer3._db import parse_recorded_at
        dt = parse_recorded_at("2026-06-05T02:42:23")
        assert dt.tzinfo == timezone.utc

    def test_already_aware_passthrough(self):
        from memstrata.layer3._db import parse_recorded_at
        dt = parse_recorded_at("2026-06-05T02:42:23+00:00")
        assert dt.tzinfo is not None

    def test_subtraction_does_not_raise(self):
        from memstrata.layer3._db import parse_recorded_at
        dt = parse_recorded_at("2026-06-05 02:42:23")
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        assert age >= 0
