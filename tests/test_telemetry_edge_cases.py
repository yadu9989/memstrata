"""Phase 33 — Telemetry edge-case aggregation tests.

Covers:
  - Missing / empty / whitespace-only external_session_id
  - Malformed external_session_id (SQL injection, too long, bad characters)
  - Provider-absent and whitespace-only provider edge cases
  - Orphaned FK references (turns with a chat_session_id pointing to no row)
  - is_valid_external_session_id unit tests (all provider ID formats)
  - Concurrent upsert race-safety (threading)
  - Sequential idempotency: same (provider, ext_id) → always same internal ID
  - turn_count accuracy under repeated upserts
  - Dashboard endpoints (/api/dashboard/state, /api/dashboard/sessions) integrity
    after edge-case ingestion
"""
from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures (mirrors test_api_server.py isolation pattern)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ML_DB_PATH", str(tmp_path / "test_edge.db"))


@pytest.fixture
def client(isolated_db):
    from memstrata.layer3.api_server import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_conn(tmp_path):
    path = tmp_path / "test_edge.db"
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post(client: TestClient, **kwargs) -> dict:
    payload = {
        "session_id": kwargs.pop("session_id", "ses_edge_001"),
        "turn_id":    kwargs.pop("turn_id", 1),
        "project_id": kwargs.pop("project_id", "proj_edge"),
        **kwargs,
    }
    r = client.post("/telemetry/session", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _chat_session_count(db_conn: sqlite3.Connection) -> int:
    return db_conn.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0]


def _telemetry_row(db_conn: sqlite3.Connection) -> sqlite3.Row:
    return db_conn.execute("SELECT * FROM telemetry_session_timeline").fetchone()


# ---------------------------------------------------------------------------
# §1 — is_valid_external_session_id unit tests
# ---------------------------------------------------------------------------

class TestValidationFunction:
    @pytest.fixture(autouse=True)
    def _import(self):
        from memstrata.layer3._db import is_valid_external_session_id
        self.valid = is_valid_external_session_id

    def test_uuid_style_accepted(self):
        assert self.valid("3f4e5d6c-7b8a-9012-cdef-3456789abcde")

    def test_numeric_only_accepted(self):
        # Meta.ai pattern: /c/([0-9]+)
        assert self.valid("123456789")

    def test_alphanumeric_accepted(self):
        # Perplexity/Grok style
        assert self.valid("4fb8pRzqKqGPnmSy")

    def test_lowercase_hex_and_hyphens_accepted(self):
        # Claude.ai / ChatGPT patterns
        assert self.valid("abcdef01-2345-6789-abcd-ef0123456789")

    def test_dot_in_id_accepted(self):
        assert self.valid("session.v2.abc123")

    def test_256_char_boundary_accepted(self):
        assert self.valid("a" * 256)

    def test_257_chars_rejected(self):
        assert not self.valid("a" * 257)

    def test_empty_string_rejected(self):
        assert not self.valid("")

    def test_space_rejected(self):
        assert not self.valid("abc def")

    def test_newline_rejected(self):
        assert not self.valid("abc\ndef")

    def test_tab_rejected(self):
        assert not self.valid("abc\tdef")

    def test_null_byte_rejected(self):
        assert not self.valid("abc\x00def")

    def test_single_quote_rejected(self):
        # SQL injection probe: 'OR'1'='1
        assert not self.valid("' OR '1'='1")

    def test_double_quote_rejected(self):
        assert not self.valid('abc"def')

    def test_semicolon_rejected(self):
        assert not self.valid("abc;DROP TABLE chat_sessions;--")

    def test_slash_rejected(self):
        assert not self.valid("abc/def")

    def test_backslash_rejected(self):
        assert not self.valid("abc\\def")

    def test_angle_bracket_rejected(self):
        assert not self.valid("<script>")

    def test_at_sign_rejected(self):
        assert not self.valid("user@host")

    def test_unicode_rejected(self):
        assert not self.valid("sesión-🔑")


# ---------------------------------------------------------------------------
# §2 — Missing / empty / whitespace external_session_id
# ---------------------------------------------------------------------------

class TestMissingExternalSessionId:
    def test_none_ext_id_stored_null_chat_session(self, client, db_conn):
        _post(client, provider="openai")
        assert _chat_session_count(db_conn) == 0
        assert _telemetry_row(db_conn)["chat_session_id"] is None

    def test_empty_string_ext_id_stored_null_chat_session(self, client, db_conn):
        _post(client, external_session_id="", provider="openai")
        assert _chat_session_count(db_conn) == 0

    def test_whitespace_only_ext_id_stored_null_chat_session(self, client, db_conn):
        _post(client, external_session_id="   ", provider="openai")
        assert _chat_session_count(db_conn) == 0

    def test_tab_only_ext_id_stored_null_chat_session(self, client, db_conn):
        _post(client, external_session_id="\t\t", provider="openai")
        assert _chat_session_count(db_conn) == 0

    def test_turn_is_stored_even_when_ext_id_missing(self, client, db_conn):
        _post(client, provider="anthropic", text="no ext id turn")
        row = _telemetry_row(db_conn)
        assert row is not None
        assert row["text"] == "no ext id turn"


# ---------------------------------------------------------------------------
# §3 — Malformed external_session_id (validation rejection)
# ---------------------------------------------------------------------------

class TestMalformedExternalSessionId:
    def test_sql_injection_probe_no_chat_session(self, client, db_conn):
        _post(client, external_session_id="' OR '1'='1", provider="openai")
        assert _chat_session_count(db_conn) == 0

    def test_semicolon_injection_no_chat_session(self, client, db_conn):
        _post(client,
              external_session_id="x; DROP TABLE chat_sessions; --",
              provider="openai")
        assert _chat_session_count(db_conn) == 0

    def test_overlong_id_no_chat_session(self, client, db_conn):
        _post(client, external_session_id="a" * 300, provider="anthropic")
        assert _chat_session_count(db_conn) == 0

    def test_newline_in_id_no_chat_session(self, client, db_conn):
        _post(client, external_session_id="abc\ndef", provider="google")
        assert _chat_session_count(db_conn) == 0

    def test_null_byte_in_id_no_chat_session(self, client, db_conn):
        _post(client, external_session_id="abc\x00def", provider="openai")
        assert _chat_session_count(db_conn) == 0

    def test_unicode_emoji_in_id_no_chat_session(self, client, db_conn):
        _post(client, external_session_id="session-🔑-abc", provider="openai")
        assert _chat_session_count(db_conn) == 0

    def test_malformed_ext_id_turn_still_stored(self, client, db_conn):
        _post(client, external_session_id="bad<chars>", provider="openai",
              actual_input_tokens=500)
        row = _telemetry_row(db_conn)
        assert row is not None
        assert row["actual_input_tokens"] == 500
        assert row["chat_session_id"] is None


# ---------------------------------------------------------------------------
# §4 — Provider edge cases
# ---------------------------------------------------------------------------

class TestProviderEdgeCases:
    def test_none_provider_with_valid_ext_id_no_chat_session(self, client, db_conn):
        _post(client, external_session_id="valid-ext-001")
        assert _chat_session_count(db_conn) == 0

    def test_empty_provider_with_valid_ext_id_no_chat_session(self, client, db_conn):
        _post(client, external_session_id="valid-ext-002", provider="")
        assert _chat_session_count(db_conn) == 0

    def test_whitespace_only_provider_no_chat_session(self, client, db_conn):
        _post(client, external_session_id="valid-ext-003", provider="   ")
        assert _chat_session_count(db_conn) == 0

    def test_valid_provider_and_ext_id_creates_chat_session(self, client, db_conn):
        _post(client, external_session_id="valid-ext-004", provider="anthropic")
        assert _chat_session_count(db_conn) == 1


# ---------------------------------------------------------------------------
# §5 — Orphaned FK reference (turn with non-existent chat_session_id)
# ---------------------------------------------------------------------------

class TestOrphanedFkReference:
    def test_dashboard_sessions_tolerates_orphaned_fk_turn(self, client, db_conn):
        """Insert a turn with a chat_session_id that has no matching chat_sessions row."""
        # Bypass the API to inject an orphaned FK directly
        db_conn.execute("PRAGMA foreign_keys = OFF")
        db_conn.execute(
            """
            INSERT INTO telemetry_session_timeline
                (session_id, turn_id, project_id, chat_session_id, actual_input_tokens)
            VALUES ('ses_orphan', 1, 'proj_orphan', 'cs_doesnotexist', 100)
            """
        )
        db_conn.commit()
        db_conn.execute("PRAGMA foreign_keys = ON")

        r = client.get("/api/dashboard/sessions")
        assert r.status_code == 200
        # Orphaned turn should NOT cause a 500
        data = r.json()
        assert "sessions" in data

    def test_dashboard_state_tolerates_orphaned_fk_turn(self, client, db_conn):
        db_conn.execute("PRAGMA foreign_keys = OFF")
        db_conn.execute(
            """
            INSERT INTO telemetry_session_timeline
                (session_id, turn_id, project_id, chat_session_id, actual_input_tokens)
            VALUES ('ses_orphan2', 1, 'proj_orphan', 'cs_ghost', 200)
            """
        )
        db_conn.commit()
        db_conn.execute("PRAGMA foreign_keys = ON")

        r = client.get("/api/dashboard/state")
        assert r.status_code == 200
        # Orphaned turn is excluded from the chat-session count
        assert r.json()["sessions"] == 0


# ---------------------------------------------------------------------------
# §6 — Idempotency and turn_count accuracy (sequential)
# ---------------------------------------------------------------------------

class TestUpsertIdempotency:
    def test_same_pair_returns_same_id(self, client, db_conn):
        """Two turns in the same external session must reuse the same chat_sessions row."""
        _post(client, session_id="s1", turn_id=1,
              external_session_id="idem-ext-001", provider="anthropic")
        _post(client, session_id="s1", turn_id=2,
              external_session_id="idem-ext-001", provider="anthropic")

        rows = db_conn.execute("SELECT * FROM chat_sessions").fetchall()
        assert len(rows) == 1
        assert rows[0]["turn_count"] == 2

    def test_turn_count_increments_correctly(self, client, db_conn):
        N = 5
        for i in range(N):
            _post(client, session_id="s_tc", turn_id=i + 1,
                  external_session_id="tc-ext-001", provider="openai")
        row = db_conn.execute("SELECT turn_count FROM chat_sessions").fetchone()
        assert row["turn_count"] == N

    def test_different_providers_same_ext_id_are_distinct(self, client, db_conn):
        _post(client, session_id="sA", turn_id=1,
              external_session_id="shared-id-001", provider="anthropic")
        _post(client, session_id="sB", turn_id=1,
              external_session_id="shared-id-001", provider="openai")
        rows = db_conn.execute("SELECT * FROM chat_sessions ORDER BY provider_id").fetchall()
        assert len(rows) == 2
        assert rows[0]["provider_id"] != rows[1]["provider_id"]

    def test_upsert_function_idempotency_direct(self, tmp_path):
        """Call upsert_chat_session N times sequentially — all must return the same ID."""
        from memstrata.layer3._db import init_db, upsert_chat_session

        db_path = str(tmp_path / "idem.db")
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        ids = [upsert_chat_session(conn, "anthropic", "idem-direct-001") for _ in range(6)]
        conn.close()
        assert len(set(ids)) == 1, f"Expected single ID, got: {set(ids)}"


# ---------------------------------------------------------------------------
# §7 — Concurrent upsert race safety
# ---------------------------------------------------------------------------

class TestConcurrentUpsert:
    def test_concurrent_upsert_produces_single_row(self, tmp_path):
        """N threads all upsert the same (provider, ext_id) — exactly one row results."""
        from memstrata.layer3._db import init_db, upsert_chat_session

        db_path = str(tmp_path / "conc.db")

        setup = sqlite3.connect(db_path, timeout=10.0)
        setup.row_factory = sqlite3.Row
        init_db(setup)
        setup.close()

        N = 8
        results: list[str] = []
        errors:  list[str] = []
        lock = threading.Lock()

        def worker():
            c = sqlite3.connect(db_path, check_same_thread=False, timeout=15.0)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 10000")
            try:
                sid = upsert_chat_session(c, "openai", "conc-shared-001")
                with lock:
                    results.append(sid)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(str(exc))
            finally:
                c.close()

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Worker errors during concurrent upsert: {errors}"
        assert len(results) == N, "Every worker must return a result"
        assert len(set(results)) == 1, f"All workers must agree on one ID; got {set(results)}"

        verify = sqlite3.connect(db_path, timeout=10.0)
        verify.row_factory = sqlite3.Row
        row = verify.execute("SELECT * FROM chat_sessions").fetchone()
        verify.close()

        assert row is not None, "chat_sessions row must exist after concurrent upserts"
        assert row["turn_count"] == N, (
            f"Expected turn_count={N}, got {row['turn_count']} — "
            "some concurrent increments were lost"
        )

    def test_concurrent_distinct_sessions_no_cross_contamination(self, tmp_path):
        """N threads each upsert a DIFFERENT session — N distinct rows must result."""
        from memstrata.layer3._db import init_db, upsert_chat_session

        db_path = str(tmp_path / "conc2.db")
        setup = sqlite3.connect(db_path, timeout=10.0)
        setup.row_factory = sqlite3.Row
        init_db(setup)
        setup.close()

        N = 6
        results: list[str] = []
        errors:  list[str] = []
        lock = threading.Lock()

        def worker(idx: int):
            c = sqlite3.connect(db_path, check_same_thread=False, timeout=15.0)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 10000")
            try:
                sid = upsert_chat_session(c, "anthropic", f"distinct-ext-{idx:03d}")
                with lock:
                    results.append(sid)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(str(exc))
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Worker errors: {errors}"
        assert len(results) == N
        assert len(set(results)) == N, "Each distinct session must produce a unique ID"

        verify = sqlite3.connect(db_path, timeout=10.0)
        verify.row_factory = sqlite3.Row
        count = verify.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0]
        verify.close()
        assert count == N


# ---------------------------------------------------------------------------
# §8 — Dashboard integrity after mixed edge-case ingestion
# ---------------------------------------------------------------------------

class TestDashboardIntegrityAfterEdgeCases:
    def _seed(self, client):
        """Mix of valid turns, invalid ext_ids, and harness-only turns."""
        # Two valid chat sessions
        _post(client, session_id="v1", turn_id=1,
              external_session_id="valid-a-001", provider="anthropic",
              actual_input_tokens=1000)
        _post(client, session_id="v2", turn_id=1,
              external_session_id="valid-b-001", provider="openai",
              actual_input_tokens=2000)
        # Two invalid ext_ids (should not create chat_sessions rows)
        _post(client, session_id="bad1", turn_id=1,
              external_session_id="' OR 1=1", provider="openai",
              actual_input_tokens=500)
        _post(client, session_id="bad2", turn_id=1,
              external_session_id="a" * 300, provider="anthropic",
              actual_input_tokens=400)
        # Harness-only turn (no ext_id)
        _post(client, session_id="h1", turn_id=1,
              provider="openai", actual_input_tokens=3000)

    def test_state_counts_only_valid_chat_sessions(self, client):
        self._seed(client)
        data = client.get("/api/dashboard/state").json()
        assert data["sessions"] == 2, (
            f"Expected 2 valid sessions, got {data['sessions']}"
        )

    def test_state_turn_count_includes_only_chat_linked_turns(self, client):
        self._seed(client)
        data = client.get("/api/dashboard/state").json()
        # Only the 2 valid turns are chat-session-linked
        assert data["turns"] == 2

    def test_sessions_list_length_excludes_invalid(self, client):
        self._seed(client)
        data = client.get("/api/dashboard/sessions").json()
        chat = [s for s in data["sessions"] if s["chat_session_id"] is not None]
        assert len(chat) == 2

    def test_sessions_list_includes_harness_session(self, client):
        self._seed(client)
        data = client.get("/api/dashboard/sessions").json()
        harness = [s for s in data["sessions"] if s["chat_session_id"] is None]
        # bad1 (SQL injection) + bad2 (overlong) both fail validation and land
        # with null chat_session_id, same as the explicit harness-only turn h1.
        assert len(harness) == 3

    def test_token_totals_correct_for_valid_sessions(self, client):
        self._seed(client)
        data = client.get("/api/dashboard/sessions").json()
        ant = next(
            (s for s in data["sessions"] if s.get("external_session_id") == "valid-a-001"),
            None,
        )
        assert ant is not None
        assert ant["total_input_tokens"] == 1000

    def test_dashboard_state_input_tokens_sum(self, client):
        self._seed(client)
        data = client.get("/api/dashboard/state").json()
        # valid-a (1000) + valid-b (2000) = 3000 chat-linked tokens
        assert data["total_input_tokens"] == 3000
