"""Tests for memstrata.layer3.api_server — Phase 31.

Verifies:
  - POST /telemetry/session creates a chat_sessions row when external_session_id
    is supplied, and the chat_session_id FK is set on the telemetry row.
  - Subsequent turns with the same (provider, external_session_id) upsert the
    row (update last_seen, increment turn_count) without creating duplicates.
  - GET /context/for-chat returns only the turns belonging to the requested
    session (cross-session isolation).
  - Session A's content does NOT appear in session B's context.
  - scope=provider expands retrieval to all sessions for the same provider.
  - Telemetry without external_session_id (harness calls) is stored with a
    null chat_session_id and does not create a spurious chat_sessions row.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Pro-overlay gating (V5.2-E E.3)
# ---------------------------------------------------------------------------
#
# Some tests in this file exercise endpoints and dashboard pieces that
# only exist when the memstrata-pro overlay is mounted on the app
# (``memstrata_pro.api_overlay.mount``). On the Open-only daemon
# the overlay isn't present, so the relevant routes and HTML are
# absent. Decorate those tests with ``@pytest.mark.requires_pro_overlay``
# and the autouse fixture below skips them when the overlay isn't
# mounted.
#
# This keeps the tests in the public repo as living documentation of
# the Pro capabilities, and they run automatically when the same
# suite is exercised inside the memstrata-pro repo.


def _pro_overlay_mounted() -> bool:
    """Return True when the api_server app has had the Pro overlay mounted."""
    from memstrata.layer3.api_server import app
    return hasattr(app.state, "cohort_api")


@pytest.fixture(autouse=True)
def _skip_if_pro_overlay_missing(request):
    """Skip tests marked ``requires_pro_overlay`` when running Open-only."""
    if request.node.get_closest_marker("requires_pro_overlay"):
        if not _pro_overlay_mounted():
            pytest.skip(
                "Test requires memstrata-pro overlay; running on Open-only daemon."
            )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Redirect every test to its own fresh SQLite file via environment variable."""
    monkeypatch.setenv("ML_DB_PATH", str(tmp_path / "test_core.db"))


@pytest.fixture
def client(isolated_db):
    # Lazy import so the env var is already set when the module's lifespan runs
    from memstrata.layer3.api_server import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_conn(tmp_path, isolated_db):
    """Direct connection to the test DB for verification queries.

    Depends on ``isolated_db`` explicitly so this connection opens
    AFTER the env var is set and any lifespan-managed connection has
    released its WAL+SHM files (Windows file-locking edge case). Loads
    sqlite-vec on the connection so verification queries against vec0
    virtual tables succeed. Wraps the yield in try/finally so a
    test-side exception still releases the file lock — that's the
    failure mode that surfaces as a Windows access violation when
    tmp_path tears down.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post_turn(
    client: TestClient,
    *,
    session_id: str,
    turn_id: int,
    project_id: str = "proj_test",
    external_session_id: str | None = None,
    provider: str | None = None,
    role: str = "assistant",
    text: str = "Some response text.",
    message_id: str | None = None,
) -> dict:
    payload: dict = {
        "session_id": session_id,
        "turn_id": turn_id,
        "project_id": project_id,
        "role": role,
        "text": text,
        "char_count": len(text),
    }
    if external_session_id is not None:
        payload["external_session_id"] = external_session_id
    if provider is not None:
        payload["provider"] = provider
    if message_id is not None:
        payload["message_id"] = message_id
    r = client.post("/telemetry/session", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_alive(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "alive"


# ---------------------------------------------------------------------------
# POST /telemetry/session — chat_sessions upsert
# ---------------------------------------------------------------------------

class TestTelemetrySessionIngest:
    def test_creates_chat_sessions_row_when_external_session_id_provided(
        self, client, db_conn
    ):
        _post_turn(
            client,
            session_id="ml-ext-001",
            turn_id=1,
            external_session_id="chatgpt-abc123",
            provider="openai",
        )
        rows = db_conn.execute("SELECT * FROM chat_sessions").fetchall()
        assert len(rows) == 1
        assert rows[0]["provider_id"] == "openai"
        assert rows[0]["external_session_id"] == "chatgpt-abc123"
        assert rows[0]["turn_count"] == 1

    def test_chat_session_id_fk_set_on_telemetry_row(self, client, db_conn):
        _post_turn(
            client,
            session_id="ml-ext-002",
            turn_id=1,
            external_session_id="claude-xyz789",
            provider="anthropic",
            text="Hello from Claude",
        )
        cs = db_conn.execute("SELECT id FROM chat_sessions").fetchone()
        assert cs is not None
        tst = db_conn.execute(
            "SELECT chat_session_id, text FROM telemetry_session_timeline"
        ).fetchone()
        assert tst["chat_session_id"] == cs["id"]
        assert tst["text"] == "Hello from Claude"

    def test_upserts_on_same_provider_and_external_session_id(self, client, db_conn):
        # Two turns in the same external session → same chat_sessions row
        _post_turn(client, session_id="ml-ext-003", turn_id=1,
                   external_session_id="gemini-sess-001", provider="google",
                   text="Turn one")
        _post_turn(client, session_id="ml-ext-003", turn_id=2,
                   external_session_id="gemini-sess-001", provider="google",
                   text="Turn two")
        rows = db_conn.execute("SELECT * FROM chat_sessions").fetchall()
        assert len(rows) == 1, "two turns in same session must not create duplicate rows"
        assert rows[0]["turn_count"] == 2

    def test_separate_external_sessions_create_separate_rows(self, client, db_conn):
        _post_turn(client, session_id="ml-ext-004", turn_id=1,
                   external_session_id="session-A", provider="anthropic")
        _post_turn(client, session_id="ml-ext-005", turn_id=1,
                   external_session_id="session-B", provider="anthropic")
        rows = db_conn.execute("SELECT id FROM chat_sessions ORDER BY first_seen").fetchall()
        assert len(rows) == 2
        assert rows[0]["id"] != rows[1]["id"]

    def test_no_chat_sessions_row_when_external_session_id_absent(self, client, db_conn):
        # Harness-style call: no external_session_id
        _post_turn(client, session_id="ses_harness001", turn_id=1,
                   provider="openai")
        rows = db_conn.execute("SELECT * FROM chat_sessions").fetchall()
        assert len(rows) == 0

    def test_chat_session_id_null_when_external_session_id_absent(self, client, db_conn):
        _post_turn(client, session_id="ses_harness002", turn_id=1,
                   provider="anthropic")
        row = db_conn.execute(
            "SELECT chat_session_id FROM telemetry_session_timeline"
        ).fetchone()
        assert row["chat_session_id"] is None

    def test_degrades_when_external_session_id_present_but_provider_absent(
        self, client, db_conn
    ):
        # Without provider we can't scope to a chat_session — must not raise
        r = client.post("/telemetry/session", json={
            "session_id": "ml-edge-001",
            "turn_id": 1,
            "project_id": "proj_x",
            "external_session_id": "some-session",  # provider omitted
            "text": "edge case turn",
        })
        assert r.status_code == 200
        assert db_conn.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# GET /context/for-chat — session-scoped retrieval (Phase 31 isolation test)
# ---------------------------------------------------------------------------

class TestContextForChat:
    def _seed_two_sessions(self, client):
        """Seed session A (anthropic) and session B (anthropic) with distinct text."""
        _post_turn(client, session_id="ml-A-001", turn_id=1,
                   external_session_id="ext-session-A", provider="anthropic",
                   text="This is Session A turn 1")
        _post_turn(client, session_id="ml-A-001", turn_id=2,
                   external_session_id="ext-session-A", provider="anthropic",
                   text="This is Session A turn 2")
        _post_turn(client, session_id="ml-B-001", turn_id=1,
                   external_session_id="ext-session-B", provider="anthropic",
                   text="This is Session B turn 1")

    def _get_chat_session_id(self, db_conn, external_session_id: str) -> str:
        row = db_conn.execute(
            "SELECT id FROM chat_sessions WHERE external_session_id = ?",
            (external_session_id,),
        ).fetchone()
        assert row is not None, f"chat_session not found: {external_session_id}"
        return row["id"]

    def test_returns_turns_for_requested_session(self, client, db_conn):
        self._seed_two_sessions(client)
        cs_id = self._get_chat_session_id(db_conn, "ext-session-A")
        r = client.get("/context/for-chat", params={"chat_session_id": cs_id})
        assert r.status_code == 200
        data = r.json()
        assert data["turn_count"] == 2
        texts = {t["text"] for t in data["turns"]}
        assert texts == {"This is Session A turn 1", "This is Session A turn 2"}

    def test_session_isolation_no_cross_session_leakage(self, client, db_conn):
        self._seed_two_sessions(client)
        cs_id_a = self._get_chat_session_id(db_conn, "ext-session-A")
        r = client.get("/context/for-chat", params={"chat_session_id": cs_id_a})
        data = r.json()
        all_texts = {t["text"] for t in data["turns"]}
        # Session B's turn must NOT appear in session A's context
        assert "This is Session B turn 1" not in all_texts

    def test_session_b_does_not_see_session_a_content(self, client, db_conn):
        self._seed_two_sessions(client)
        cs_id_b = self._get_chat_session_id(db_conn, "ext-session-B")
        r = client.get("/context/for-chat", params={"chat_session_id": cs_id_b})
        data = r.json()
        all_texts = {t["text"] for t in data["turns"]}
        assert "This is Session A turn 1" not in all_texts
        assert "This is Session A turn 2" not in all_texts
        assert "This is Session B turn 1" in all_texts

    def test_scope_provider_expands_to_all_sessions_for_provider(self, client, db_conn):
        self._seed_two_sessions(client)
        cs_id_a = self._get_chat_session_id(db_conn, "ext-session-A")
        r = client.get("/context/for-chat", params={
            "chat_session_id": cs_id_a,
            "scope": "provider",
        })
        assert r.status_code == 200
        data = r.json()
        # Both sessions are anthropic, so all 3 turns should be returned
        assert data["turn_count"] == 3
        texts = {t["text"] for t in data["turns"]}
        assert "This is Session B turn 1" in texts

    def test_scope_provider_does_not_cross_provider_boundary(self, client, db_conn):
        # Add a deepseek session
        _post_turn(client, session_id="ml-ds-001", turn_id=1,
                   external_session_id="ds-session-X", provider="deepseek",
                   text="DeepSeek session content")
        self._seed_two_sessions(client)
        cs_id_a = self._get_chat_session_id(db_conn, "ext-session-A")
        r = client.get("/context/for-chat", params={
            "chat_session_id": cs_id_a,
            "scope": "provider",
        })
        data = r.json()
        texts = {t["text"] for t in data["turns"]}
        assert "DeepSeek session content" not in texts

    def test_returns_empty_turns_for_unknown_session(self, client):
        r = client.get("/context/for-chat", params={"chat_session_id": "cs_doesnotexist"})
        assert r.status_code == 200
        assert r.json()["turn_count"] == 0

    def test_scope_provider_404_for_unknown_chat_session_id(self, client):
        r = client.get("/context/for-chat", params={
            "chat_session_id": "cs_ghost",
            "scope": "provider",
        })
        assert r.status_code == 404

    def test_context_text_contains_turn_content(self, client, db_conn):
        _post_turn(client, session_id="ml-C-001", turn_id=1,
                   external_session_id="ext-session-C", provider="openai",
                   text="The answer is 42")
        cs_id = self._get_chat_session_id(db_conn, "ext-session-C")
        r = client.get("/context/for-chat", params={"chat_session_id": cs_id})
        assert "The answer is 42" in r.json()["text"]

    def test_turns_without_text_are_excluded(self, client, db_conn):
        # A harness-style turn (no text field) should not appear in chat context
        client.post("/telemetry/session", json={
            "session_id": "ses_harness_x",
            "turn_id": 1,
            "project_id": "proj_x",
            "external_session_id": "ext-session-D",
            "provider": "openai",
            # text intentionally omitted
            "actual_input_tokens": 500,
        })
        cs_id = self._get_chat_session_id(db_conn, "ext-session-D")
        r = client.get("/context/for-chat", params={"chat_session_id": cs_id})
        assert r.json()["turn_count"] == 0


# ---------------------------------------------------------------------------
# POST /projects/register — VS Code auto-ingestion handshake
# ---------------------------------------------------------------------------

class TestProjectRegistration:
    """V5.2-E reactivation of the dormant auto-ingestion flow."""

    def test_register_writes_opt_in_row(self, client, tmp_path, db_conn):
        project = tmp_path / "demo-proj"
        project.mkdir()

        r = client.post("/projects/register", json={"path": str(project)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "opted_in"
        assert body["project_path"] == str(project.resolve())

        row = db_conn.execute(
            "SELECT state FROM project_opt_in WHERE project_path = ?",
            (str(project.resolve()),),
        ).fetchone()
        assert row is not None and row[0] == "opted_in"

    def test_register_is_idempotent(self, client, tmp_path, db_conn):
        project = tmp_path / "demo-proj-2"
        project.mkdir()
        for _ in range(3):
            r = client.post("/projects/register", json={"path": str(project)})
            assert r.status_code == 200

        rows = db_conn.execute(
            "SELECT COUNT(*) FROM project_opt_in WHERE project_path = ?",
            (str(project.resolve()),),
        ).fetchone()
        assert rows[0] == 1

    def test_register_rejects_missing_path(self, client, tmp_path):
        ghost = tmp_path / "does-not-exist"
        r = client.post("/projects/register", json={"path": str(ghost)})
        assert r.status_code == 400
        assert "does not exist" in r.json()["detail"]

    def test_register_rejects_file_path(self, client, tmp_path):
        f = tmp_path / "not-a-dir.txt"
        f.write_text("x")
        r = client.post("/projects/register", json={"path": str(f)})
        assert r.status_code == 400
        assert "not a directory" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Session registration round-trip
# ---------------------------------------------------------------------------

class TestSessionRegistration:
    def test_register_and_close(self, client):
        r = client.post("/sessions", json={
            "session_id": "ses_reg001",
            "project_id": "proj_test",
            "started_at": "2026-06-03T10:00:00Z",
            "client_id": "memstrata-pro-harness",
        })
        assert r.status_code == 200
        assert r.json()["session_id"] == "ses_reg001"
        assert r.json()["watcher_session_id"].startswith("ws_")

        r2 = client.post("/sessions/ses_reg001/close")
        assert r2.status_code == 200
        assert r2.json()["session_id"] == "ses_reg001"
        assert "closed_at" in r2.json()


# ---------------------------------------------------------------------------
# GET /context — browser extension context endpoint
# ---------------------------------------------------------------------------

def _post_browser_turn(client, *, ext_id, provider, text, session_id=None, turn_id=1,
                       project_id="proj_test", role="assistant"):
    """Post a browser-extension turn (client_source='browser_ext') for isolation tests."""
    payload = {
        "session_id": session_id or f"ml-{ext_id}-{turn_id}",
        "turn_id": turn_id,
        "project_id": project_id,
        "external_session_id": ext_id,
        "provider": provider,
        "client_source": "browser_ext",
        "role": role,
        "text": text,
        "char_count": len(text),
    }
    r = client.post("/telemetry/session", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


class TestGetContext:
    """Project-scope (fallback) behavior — no external_session_id/provider supplied."""

    def test_returns_200_with_empty_when_no_history(self, client):
        r = client.get("/context", params={"project_id": "default"})
        assert r.status_code == 200
        data = r.json()
        assert data["text"] == ""
        assert data["token_count"] == 0
        assert data["project_id"] == "default"
        assert data["scope"] == "project"

    def test_default_project_id_never_404(self, client):
        r = client.get("/context")
        assert r.status_code == 200
        assert r.json()["project_id"] == "default"

    def test_returns_context_after_turns_recorded(self, client):
        # client_source defaults to NULL (legacy harness behavior) → project scope
        _post_turn(client, session_id="ctx-001", turn_id=1,
                   project_id="proj_test", text="Hello from the AI", role="assistant")
        r = client.get("/context", params={"project_id": "proj_test"})
        assert r.status_code == 200
        data = r.json()
        assert "Hello from the AI" in data["text"]
        assert data["token_count"] > 0
        assert data["project_id"] == "proj_test"

    def test_context_text_includes_role_prefix(self, client):
        _post_turn(client, session_id="ctx-002", turn_id=1,
                   project_id="proj_test", text="Role test content", role="user")
        r = client.get("/context", params={"project_id": "proj_test"})
        assert "[USER] Role test content" in r.json()["text"]

    def test_deduplicates_identical_text(self, client):
        _post_turn(client, session_id="ctx-003", turn_id=1,
                   project_id="proj_test", text="Repeated text")
        _post_turn(client, session_id="ctx-003", turn_id=2,
                   project_id="proj_test", text="Repeated text")
        r = client.get("/context", params={"project_id": "proj_test"})
        assert r.json()["text"].count("Repeated text") == 1

    def test_project_isolation(self, client):
        _post_turn(client, session_id="ctx-004", turn_id=1,
                   project_id="proj_A", text="Project A content")
        r = client.get("/context", params={"project_id": "proj_B"})
        assert r.status_code == 200
        assert r.json()["text"] == ""
        assert r.json()["token_count"] == 0

    def test_token_count_positive_when_text_present(self, client):
        _post_turn(client, session_id="ctx-005", turn_id=1,
                   project_id="proj_test", text="A" * 100)
        r = client.get("/context", params={"project_id": "proj_test"})
        assert r.json()["token_count"] >= 1

    def test_multiple_turns_all_in_context(self, client):
        _post_turn(client, session_id="ctx-006", turn_id=1,
                   project_id="proj_test", text="First message")
        _post_turn(client, session_id="ctx-006", turn_id=2,
                   project_id="proj_test", text="Second message")
        r = client.get("/context", params={"project_id": "proj_test"})
        text = r.json()["text"]
        assert "First message" in text
        assert "Second message" in text

    def test_turns_without_text_excluded(self, client):
        client.post("/telemetry/session", json={
            "session_id": "ctx-007",
            "turn_id": 1,
            "project_id": "proj_test",
            "actual_input_tokens": 500,
        })
        r = client.get("/context", params={"project_id": "proj_test"})
        assert r.json()["text"] == ""
        assert r.json()["token_count"] == 0

    def test_project_scope_excludes_browser_ext_turns(self, client):
        """A browser-extension chat turn must never leak into harness project context."""
        _post_browser_turn(client, ext_id="leak-1", provider="anthropic",
                           text="Browser chat content", project_id="proj_test")
        r = client.get("/context", params={"project_id": "proj_test"})
        assert "Browser chat content" not in r.json()["text"]
        assert r.json()["text"] == ""


class TestGetContextSessionIsolation:
    """V5.4 §2.1: each web chat thread must see only its own context."""

    def test_returns_session_scoped_context(self, client):
        _post_browser_turn(client, ext_id="chatA-abc", provider="anthropic",
                           text="Claude thread A content")
        r = client.get("/context", params={
            "external_session_id": "chatA-abc",
            "provider":            "anthropic",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["scope"] == "session"
        assert "Claude thread A content" in data["text"]
        assert data["token_count"] > 0

    def test_strict_isolation_between_two_threads_same_provider(self, client):
        """Two ChatGPT threads must NOT see each other's content."""
        _post_browser_turn(client, ext_id="gpt-aaa", provider="openai",
                           text="GPT thread AAA content")
        _post_browser_turn(client, ext_id="gpt-bbb", provider="openai",
                           text="GPT thread BBB content")

        ra = client.get("/context", params={
            "external_session_id": "gpt-aaa", "provider": "openai",
        })
        rb = client.get("/context", params={
            "external_session_id": "gpt-bbb", "provider": "openai",
        })
        assert "GPT thread AAA content" in ra.json()["text"]
        assert "GPT thread BBB content" not in ra.json()["text"]
        assert "GPT thread BBB content" in rb.json()["text"]
        assert "GPT thread AAA content" not in rb.json()["text"]

    def test_strict_isolation_across_providers(self, client):
        """Same external_session_id under different providers must not collide."""
        _post_browser_turn(client, ext_id="dup-id", provider="anthropic",
                           text="Anthropic side content")
        _post_browser_turn(client, ext_id="dup-id", provider="openai",
                           text="OpenAI side content")

        ra = client.get("/context", params={
            "external_session_id": "dup-id", "provider": "anthropic",
        })
        ro = client.get("/context", params={
            "external_session_id": "dup-id", "provider": "openai",
        })
        assert "Anthropic side content" in ra.json()["text"]
        assert "OpenAI side content" not in ra.json()["text"]
        assert "OpenAI side content" in ro.json()["text"]
        assert "Anthropic side content" not in ro.json()["text"]

    def test_brand_new_thread_returns_empty_200(self, client):
        """A chat with no recorded history yet must return 200 with empty context."""
        r = client.get("/context", params={
            "external_session_id": "brand-new-thread-xyz",
            "provider":            "openai",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["text"] == ""
        assert data["token_count"] == 0
        assert data["scope"] == "session"

    def test_session_scope_excludes_harness_turns(self, client):
        """Harness ingestion (client_source NULL) must never appear in session context."""
        _post_turn(client, session_id="harness-1", turn_id=1,
                   project_id="proj_test", text="Harness coding content",
                   external_session_id="chatX", provider="openai")
        # Same provider+external_session_id but harness-origin, no client_source
        # — must NOT appear when the browser-ext fetches that session's context.
        r = client.get("/context", params={
            "external_session_id": "chatX",
            "provider":            "openai",
        })
        assert "Harness coding content" not in r.json()["text"]

    def test_invalid_external_session_id_returns_empty_safe(self, client):
        """Malicious-looking ext_id must return empty, never raise."""
        r = client.get("/context", params={
            "external_session_id": "'; DROP TABLE chat_sessions;--",
            "provider":            "openai",
        })
        assert r.status_code == 200
        assert r.json()["text"] == ""
        assert r.json()["token_count"] == 0

    def test_provider_alone_falls_back_to_project_scope(self, client):
        """provider without external_session_id is treated as project-scope fallback."""
        _post_turn(client, session_id="prov-1", turn_id=1,
                   project_id="proj_test", text="Project content")
        r = client.get("/context", params={
            "provider":   "openai",
            "project_id": "proj_test",
        })
        assert r.json()["scope"] == "project"
        assert "Project content" in r.json()["text"]

    def test_dedup_within_session(self, client):
        _post_browser_turn(client, ext_id="dedup-s", provider="anthropic",
                           text="Same line", turn_id=1)
        _post_browser_turn(client, ext_id="dedup-s", provider="anthropic",
                           text="Same line", turn_id=2)
        r = client.get("/context", params={
            "external_session_id": "dedup-s",
            "provider":            "anthropic",
        })
        assert r.json()["text"].count("Same line") == 1


# ---------------------------------------------------------------------------
# Phase 32 — new columns: baseline_no_context, injected, cache_hit_estimated
# ---------------------------------------------------------------------------

class TestPhase32Columns:
    def test_stores_baseline_no_context_and_injected(self, client, db_conn):
        client.post("/telemetry/session", json={
            "session_id": "ses_p32_001",
            "turn_id": 1,
            "project_id": "proj_p32",
            "external_session_id": "p32-ext-001",
            "provider": "anthropic",
            "actual_input_tokens": 1200,
            "actual_output_tokens": 300,
            "baseline_no_context": 800,
            "injected": True,
            "cache_hit_estimated": False,
        })
        row = db_conn.execute("SELECT * FROM telemetry_session_timeline").fetchone()
        assert row["baseline_no_context"] == 800
        assert row["injected"] == 1
        assert row["cache_hit_estimated"] == 0

    def test_stores_cache_hit_estimated(self, client, db_conn):
        client.post("/telemetry/session", json={
            "session_id": "ses_p32_002",
            "turn_id": 2,
            "project_id": "proj_p32",
            "external_session_id": "p32-ext-001",
            "provider": "anthropic",
            "actual_input_tokens": 850,
            "actual_output_tokens": 200,
            "baseline_no_context": 800,
            "injected": False,
            "cache_hit_estimated": True,
        })
        row = db_conn.execute("SELECT * FROM telemetry_session_timeline").fetchone()
        assert row["cache_hit_estimated"] == 1
        assert row["injected"] == 0

    def test_new_columns_default_to_zero_when_omitted(self, client, db_conn):
        client.post("/telemetry/session", json={
            "session_id": "ses_p32_003",
            "turn_id": 1,
            "project_id": "proj_p32",
        })
        row = db_conn.execute("SELECT * FROM telemetry_session_timeline").fetchone()
        assert row["baseline_no_context"] is None
        assert row["injected"] == 0
        assert row["cache_hit_estimated"] == 0


# ---------------------------------------------------------------------------
# Phase 32 — Dashboard endpoints
# ---------------------------------------------------------------------------

def _seed_dashboard(client):
    """Seed two chat sessions (one anthropic, one openai) plus a harness session."""
    # Anthropic session: 2 turns, 1 injected, 1 cached
    for turn_id, injected, cache_hit, n_in, n_out in [
        (1, True,  False, 1500, 400),
        (2, False, True,  900,  250),
    ]:
        client.post("/telemetry/session", json={
            "session_id": "ses_dash_ant",
            "turn_id": turn_id,
            "project_id": "proj_d",
            "external_session_id": "ant-session-dash",
            "provider": "anthropic",
            "actual_input_tokens": n_in,
            "actual_output_tokens": n_out,
            "baseline_no_context": 800,
            "injected": injected,
            "cache_hit_estimated": cache_hit,
        })
    # OpenAI session: 1 turn, injected
    client.post("/telemetry/session", json={
        "session_id": "ses_dash_oai",
        "turn_id": 1,
        "project_id": "proj_d",
        "external_session_id": "oai-session-dash",
        "provider": "openai",
        "actual_input_tokens": 2000,
        "actual_output_tokens": 500,
        "baseline_no_context": 1200,
        "injected": True,
        "cache_hit_estimated": False,
    })
    # Harness-only session (no external_session_id)
    client.post("/telemetry/session", json={
        "session_id": "ses_harness_dash",
        "turn_id": 1,
        "project_id": "proj_d",
        "provider": "openai",
        "actual_input_tokens": 3000,
        "actual_output_tokens": 700,
        "injected": True,
        "cache_hit_estimated": False,
    })


class TestDashboardState:
    def test_returns_expected_keys(self, client):
        r = client.get("/api/dashboard/state")
        assert r.status_code == 200
        data = r.json()
        for key in ("status", "sessions", "turns", "total_input_tokens",
                    "injected_turns", "cache_hit_turns",
                    "injection_rate_pct", "cache_hit_rate_pct",
                    "savings_pct", "recall_pct"):
            assert key in data, f"missing key: {key}"

    def test_counts_sessions_and_turns(self, client):
        _seed_dashboard(client)
        data = client.get("/api/dashboard/state").json()
        # 2 chat sessions (anthropic + openai); harness session excluded
        assert data["sessions"] == 2
        # 3 chat-linked turns total (2 anthropic + 1 openai)
        assert data["turns"] == 3

    def test_aggregates_injection_and_cache(self, client):
        _seed_dashboard(client)
        data = client.get("/api/dashboard/state").json()
        assert data["injected_turns"] == 2   # turn1-ant + turn1-oai (harness excluded)
        assert data["cache_hit_turns"] == 1  # turn2-ant

    def test_empty_db_returns_zeros(self, client):
        data = client.get("/api/dashboard/state").json()
        assert data["sessions"] == 0
        assert data["turns"] == 0
        assert data["savings_pct"] == 0.0


class TestDashboardSessions:
    def test_returns_sessions_key(self, client):
        r = client.get("/api/dashboard/sessions")
        assert r.status_code == 200
        assert "sessions" in r.json()
        assert "total_count" in r.json()

    def test_chat_sessions_appear_grouped(self, client):
        _seed_dashboard(client)
        data = client.get("/api/dashboard/sessions").json()
        chat = [s for s in data["sessions"] if s["chat_session_id"] is not None]
        providers = {s["provider_id"] for s in chat}
        assert "anthropic" in providers
        assert "openai" in providers

    def test_harness_sessions_have_null_chat_session_id(self, client):
        _seed_dashboard(client)
        data = client.get("/api/dashboard/sessions").json()
        harness = [s for s in data["sessions"] if s["chat_session_id"] is None]
        assert len(harness) == 1
        assert harness[0]["external_session_id"] == "ses_harness_dash"

    def test_per_session_token_totals(self, client):
        _seed_dashboard(client)
        data = client.get("/api/dashboard/sessions").json()
        ant = next(s for s in data["sessions"] if s.get("external_session_id") == "ant-session-dash")
        assert ant["total_input_tokens"] == 1500 + 900
        assert ant["total_output_tokens"] == 400 + 250
        assert ant["injected_turns"] == 1
        assert ant["cache_hit_turns"] == 1

    def test_per_session_turn_count(self, client):
        _seed_dashboard(client)
        data = client.get("/api/dashboard/sessions").json()
        oai = next(s for s in data["sessions"] if s.get("external_session_id") == "oai-session-dash")
        assert oai["turn_count"] == 1
        assert oai["total_input_tokens"] == 2000

    def test_empty_db_returns_empty_list(self, client):
        data = client.get("/api/dashboard/sessions").json()
        assert data["sessions"] == []
        assert data["total_count"] == 0


class TestDashboardHtml:
    def test_dashboard_returns_html(self, client):
        r = client.get("/dashboard")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "MemStrata" in r.text

    def test_dashboard_references_api_endpoints(self, client):
        r = client.get("/dashboard")
        assert "/api/dashboard/state" in r.text
        assert "/api/dashboard/sessions" in r.text

    @pytest.mark.requires_pro_overlay
    def test_dashboard_has_tab_elements(self, client):
        # Asserts the Money tab + sub-tabs + /license/plan-features fetch URL
        # are present in the dashboard HTML. Those pieces are injected by
        # the Pro overlay's dashboard_extras substitution and are absent
        # on Open-only.
        r = client.get("/dashboard")
        assert 'id="tabs"' in r.text
        # V5.3 top-level tabs
        assert 'data-tab="money"' in r.text
        assert 'data-tab="now"' in r.text
        assert 'data-tab="quality"' in r.text
        # V5.4 Money sub-tabs
        assert 'data-mtab="chat"' in r.text
        assert 'data-mtab="coding"' in r.text
        assert 'id="mtab-coding"' in r.text
        # Plan gating endpoint
        assert '/license/plan-features' in r.text


# ---------------------------------------------------------------------------
# Phase 32 — DELETE endpoints  (NL command interceptor backend)
# ---------------------------------------------------------------------------

class TestDeleteChatSession:
    def _seed_session(self, client, ext_session_id: str, provider: str = "anthropic") -> str:
        """Seed a chat session with one turn; return the internal chat_session_id."""
        _post_turn(
            client,
            session_id="ml-del-001",
            turn_id=1,
            external_session_id=ext_session_id,
            provider=provider,
            text="Some captured turn",
        )
        r = client.get("/api/dashboard/sessions")
        sessions = r.json()["sessions"]
        cs = next((s for s in sessions if s["external_session_id"] == ext_session_id), None)
        assert cs is not None
        return cs["chat_session_id"]

    def test_delete_chat_session_removes_rows(self, client, db_conn):
        cs_id = self._seed_session(client, "del-ext-001")
        r = client.post("/chat-session/delete", json={"chat_session_id": cs_id})
        assert r.status_code == 200
        data = r.json()
        assert data["deleted"] is True
        assert data["telemetry_rows_deleted"] >= 1
        assert data["session_row_deleted"] == 1

        # Verify DB is clean
        assert db_conn.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0] == 0
        rows = db_conn.execute(
            "SELECT COUNT(*) FROM telemetry_session_timeline WHERE chat_session_id = ?",
            (cs_id,),
        ).fetchone()[0]
        assert rows == 0

    def test_delete_chat_session_leaves_other_sessions_intact(self, client, db_conn):
        _post_turn(client, session_id="ml-del-002", turn_id=1,
                   external_session_id="del-ext-A", provider="anthropic", text="Session A")
        _post_turn(client, session_id="ml-del-003", turn_id=1,
                   external_session_id="del-ext-B", provider="anthropic", text="Session B")
        cs_a = db_conn.execute(
            "SELECT id FROM chat_sessions WHERE external_session_id = ?", ("del-ext-A",)
        ).fetchone()["id"]
        client.post("/chat-session/delete", json={"chat_session_id": cs_a})
        # Session B must still exist
        remaining = db_conn.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0]
        assert remaining == 1

    def test_delete_missing_session_returns_deleted_false_counts(self, client):
        r = client.post("/chat-session/delete", json={"chat_session_id": "cs_nonexistent"})
        assert r.status_code == 200
        data = r.json()
        assert data["session_row_deleted"] == 0

    def test_delete_empty_chat_session_id_returns_400(self, client):
        r = client.post("/chat-session/delete", json={"chat_session_id": ""})
        assert r.status_code == 400


class TestDeleteAllMemory:
    def _seed_multiple(self, client):
        for i, (ext, prov) in enumerate([("bulk-A", "anthropic"), ("bulk-B", "openai")]):
            _post_turn(client, session_id=f"ml-bulk-{i}", turn_id=1,
                       external_session_id=ext, provider=prov, text=f"Bulk turn {i}")

    def test_delete_all_clears_everything(self, client, db_conn):
        self._seed_multiple(client)
        r = client.post("/memory/delete-all")
        assert r.status_code == 200
        data = r.json()
        assert data["deleted"] is True
        assert data["session_rows_deleted"] == 2
        assert data["telemetry_rows_deleted"] == 2
        assert db_conn.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0] == 0
        assert db_conn.execute("SELECT COUNT(*) FROM telemetry_session_timeline").fetchone()[0] == 0

    def test_delete_all_on_empty_db_succeeds(self, client):
        r = client.post("/memory/delete-all")
        assert r.status_code == 200
        assert r.json()["deleted"] is True


# ---------------------------------------------------------------------------
# Phase 33 — Plan-feature endpoints
# ---------------------------------------------------------------------------

@pytest.mark.requires_pro_overlay
class TestPlanFeatureEndpoints:
    def test_get_current_plan_returns_plan(self, client):
        r = client.get("/license/current-plan")
        assert r.status_code == 200
        assert "plan" in r.json()
        # Fresh DB defaults to 'trial'
        assert r.json()["plan"] == "trial"

    def test_get_plan_features_returns_features_list(self, client):
        r = client.get("/license/plan-features")
        assert r.status_code == 200
        data = r.json()
        assert "plan" in data
        assert "features" in data
        assert isinstance(data["features"], list)
        # trial plan should include harness
        assert "harness" in data["features"]

    def test_set_plan_changes_active_plan(self, client):
        r = client.post("/license/set-plan", json={"plan": "lite"})
        assert r.status_code == 200
        assert r.json()["plan"] == "lite"
        # Verify the change is reflected
        r2 = client.get("/license/current-plan")
        assert r2.json()["plan"] == "lite"

    def test_lite_plan_excludes_harness_from_features(self, client):
        client.post("/license/set-plan", json={"plan": "lite"})
        r = client.get("/license/plan-features")
        features = r.json()["features"]
        assert "browser_ext" in features
        assert "money_tab_chat_only" in features
        assert "harness" not in features
        assert "vscode_ext" not in features

    def test_set_plan_unknown_returns_400(self, client):
        r = client.post("/license/set-plan", json={"plan": "enterprise"})
        assert r.status_code == 400

    def test_free_plan_excludes_browser_ext(self, client):
        client.post("/license/set-plan", json={"plan": "free"})
        r = client.get("/license/plan-features")
        features = r.json()["features"]
        assert "browser_ext" not in features
        assert "mcp_server" in features
        assert "local_dashboard" in features


# ---------------------------------------------------------------------------
# Stream-pause dedup: message_id UPSERT (V5.3 StreamWatcher fix)
# ---------------------------------------------------------------------------

class TestMessageIdUpsert:
    """Verify that supplying a message_id makes the backend UPSERT rather than
    INSERT so a mid-stream pause that fires onComplete twice produces exactly
    one telemetry row with the latest text, not two duplicate rows.
    """

    def test_same_message_id_updates_existing_row(self, client, db_conn):
        # First POST: partial text (stream paused early)
        _post_turn(
            client, session_id="ml-mid-001", turn_id=1,
            message_id="mlmsg-111-aaa", text="Partial response",
        )
        # Second POST: full text (model resumed and onComplete fired again)
        _post_turn(
            client, session_id="ml-mid-001", turn_id=1,
            message_id="mlmsg-111-aaa", text="Full response text after resume",
        )
        rows = db_conn.execute(
            "SELECT COUNT(*) FROM telemetry_session_timeline"
        ).fetchone()[0]
        assert rows == 1, "same message_id must UPSERT, not insert a duplicate row"

        row = db_conn.execute("SELECT text, char_count FROM telemetry_session_timeline").fetchone()
        assert row["text"] == "Full response text after resume"
        assert row["char_count"] == len("Full response text after resume")

    def test_different_message_ids_create_separate_rows(self, client, db_conn):
        _post_turn(
            client, session_id="ml-mid-002", turn_id=1,
            message_id="mlmsg-222-bbb", text="Turn one",
        )
        _post_turn(
            client, session_id="ml-mid-002", turn_id=2,
            message_id="mlmsg-222-ccc", text="Turn two",
        )
        rows = db_conn.execute(
            "SELECT COUNT(*) FROM telemetry_session_timeline"
        ).fetchone()[0]
        assert rows == 2, "different message_ids must produce separate rows"

    def test_null_message_id_always_inserts(self, client, db_conn):
        # Harness-style calls without message_id must never be deduplicated
        _post_turn(client, session_id="ml-mid-003", turn_id=1, text="Row one")
        _post_turn(client, session_id="ml-mid-003", turn_id=1, text="Row two")
        rows = db_conn.execute(
            "SELECT COUNT(*) FROM telemetry_session_timeline"
        ).fetchone()[0]
        assert rows == 2, "NULL message_id turns must be inserted independently"

    def test_upsert_preserves_chat_session_id_from_first_insert(self, client, db_conn):
        # First POST establishes the chat session link
        _post_turn(
            client, session_id="ml-mid-004", turn_id=1,
            message_id="mlmsg-444-ddd",
            external_session_id="ext-upsert-001",
            provider="openai",
            text="Partial",
        )
        cs_row = db_conn.execute("SELECT id FROM chat_sessions").fetchone()
        assert cs_row is not None
        original_cs_id = cs_row["id"]

        # Second POST: same message_id but no external_session_id/provider —
        # chat_session_id FK must be preserved via COALESCE on the conflict clause.
        _post_turn(
            client, session_id="ml-mid-004", turn_id=1,
            message_id="mlmsg-444-ddd",
            text="Full response",
        )
        tst_row = db_conn.execute(
            "SELECT chat_session_id, text FROM telemetry_session_timeline"
        ).fetchone()
        assert tst_row["chat_session_id"] == original_cs_id, (
            "UPSERT must preserve existing chat_session_id via COALESCE"
        )
        assert tst_row["text"] == "Full response"

    def test_same_message_id_different_sessions_creates_two_rows(self, client, db_conn):
        # message_id uniqueness is scoped to session_id — same message_id in two
        # different sessions must NOT collide.
        _post_turn(
            client, session_id="ml-mid-005a", turn_id=1,
            message_id="mlmsg-shared", text="Session A",
        )
        _post_turn(
            client, session_id="ml-mid-005b", turn_id=1,
            message_id="mlmsg-shared", text="Session B",
        )
        rows = db_conn.execute(
            "SELECT COUNT(*) FROM telemetry_session_timeline"
        ).fetchone()[0]
        assert rows == 2, "same message_id in different sessions must produce separate rows"


# ---------------------------------------------------------------------------
# client_source: Chat vs Coding dashboard split (V5.4)
# ---------------------------------------------------------------------------

class TestClientSource:
    """Verify that client_source is stored correctly and the dashboard financial
    split uses client_source in preference to chat_session_id IS NULL/NOT NULL."""

    def test_browser_ext_client_source_stored(self, client, db_conn):
        # Legacy 'browser_ext' is normalized to canonical 'chat' on ingest so
        # the dashboard split has only two values to handle. See record_turn().
        r = client.post("/telemetry/session", json={
            "session_id": "cs-test-001",
            "turn_id": 1,
            "project_id": "proj_cs",
            "external_session_id": "ext-cs-001",
            "provider": "anthropic",
            "client_source": "browser_ext",
            "text": "Chat turn",
            "char_count": 9,
        })
        assert r.status_code == 200
        row = db_conn.execute(
            "SELECT client_source FROM telemetry_session_timeline"
        ).fetchone()
        assert row["client_source"] == "chat"

    def test_harness_client_source_stored(self, client, db_conn):
        # Legacy 'harness' is normalized to canonical 'coding' on ingest.
        r = client.post("/telemetry/session", json={
            "session_id": "cs-test-002",
            "turn_id": 1,
            "project_id": "proj_cs",
            "provider": "anthropic",
            "client_source": "harness",
            "actual_input_tokens": 1000,
        })
        assert r.status_code == 200
        row = db_conn.execute(
            "SELECT client_source FROM telemetry_session_timeline"
        ).fetchone()
        assert row["client_source"] == "coding"

    def test_legacy_web_client_source_normalized_to_chat(self, client, db_conn):
        # Older browser extension builds emitted 'web' for copilot.microsoft.com;
        # the ingest path normalizes it to 'chat' so it appears in the Chat tab.
        r = client.post("/telemetry/session", json={
            "session_id": "cs-test-web",
            "turn_id": 1,
            "project_id": "proj_cs",
            "provider": "copilot",
            "client_source": "web",
            "text": "Copilot turn",
            "char_count": 12,
        })
        assert r.status_code == 200
        row = db_conn.execute(
            "SELECT client_source FROM telemetry_session_timeline"
        ).fetchone()
        assert row["client_source"] == "chat"

    def test_legacy_null_client_source_defaults_to_coding(self, client, db_conn):
        # Omitting client_source (legacy harness/IDE caller) defaults to 'coding'
        # at the route handler so the row lands correctly in the Coding dashboard
        # tab instead of being miscategorized.
        r = client.post("/telemetry/session", json={
            "session_id": "cs-test-003",
            "turn_id": 1,
            "project_id": "proj_cs",
            "provider": "anthropic",
        })
        assert r.status_code == 200
        row = db_conn.execute(
            "SELECT client_source FROM telemetry_session_timeline"
        ).fetchone()
        assert row["client_source"] == "coding"

    def test_browser_ext_rows_not_in_harness_dashboard_sessions(self, client, db_conn):
        # A browser_ext turn must NOT appear in the harness/Coding sessions list.
        client.post("/telemetry/session", json={
            "session_id": "cs-test-004",
            "turn_id": 1,
            "project_id": "proj_cs",
            "external_session_id": "ext-cs-004",
            "provider": "anthropic",
            "client_source": "browser_ext",
            "text": "Chat turn from extension",
        })
        data = client.get("/api/dashboard/sessions").json()
        harness = [s for s in data["sessions"] if s["chat_session_id"] is None]
        assert len(harness) == 0, (
            "browser_ext turn must not appear in harness/Coding sessions"
        )

    def test_harness_turn_with_chat_session_id_stays_in_coding(self, client, db_conn):
        # A harness turn that happens to have a chat_session_id (unusual, but
        # possible) must be classified as Coding because client_source='harness'.
        # Seed a chat session first so the FK exists
        client.post("/telemetry/session", json={
            "session_id": "cs-test-005-ext",
            "turn_id": 1,
            "project_id": "proj_cs",
            "external_session_id": "ext-cs-005",
            "provider": "anthropic",
            "client_source": "browser_ext",
            "text": "Browser ext turn",
        })
        # Now seed a harness turn with client_source='harness' but no external_session_id
        client.post("/telemetry/session", json={
            "session_id": "cs-test-005-h",
            "turn_id": 1,
            "project_id": "proj_cs",
            "provider": "anthropic",
            "client_source": "harness",
            "actual_input_tokens": 500,
        })
        data = client.get("/api/dashboard/sessions").json()
        harness = [s for s in data["sessions"] if s["chat_session_id"] is None]
        assert len(harness) == 1, "harness turn must appear in Coding sessions"


# ---------------------------------------------------------------------------
# Phase 21 bug fixes — turn counter, baseline picker, output savings
# ---------------------------------------------------------------------------

class TestTurnCountInflation:
    """Regression: chat_sessions.turn_count must NOT advance when a duplicate
    (session_id, message_id) POST UPSERTs the existing row."""

    def test_upsert_update_does_not_inflate_turn_count(self, client, db_conn):
        payload = {
            "session_id": "sess-upsert-1",
            "turn_id": 1,
            "project_id": "proj_inflate",
            "external_session_id": "ext-inflate-1",
            "provider": "anthropic",
            "client_source": "chat",
            "message_id": "msg-A",
            "role": "assistant",
            "text": "partial...",
            "char_count": 10,
        }
        # First POST — fresh insert.
        r1 = client.post("/telemetry/session", json=payload)
        assert r1.status_code == 200

        # Second POST — same (session_id, message_id) but updated text, simulating
        # a stream-pause re-fire. This must UPSERT the existing row.
        payload2 = dict(payload, text="final text", char_count=10)
        r2 = client.post("/telemetry/session", json=payload2)
        assert r2.status_code == 200

        # Exactly one telemetry row.
        rows = db_conn.execute(
            "SELECT COUNT(*) FROM telemetry_session_timeline WHERE session_id=? AND message_id=?",
            ("sess-upsert-1", "msg-A"),
        ).fetchone()[0]
        assert rows == 1, "UPSERT must not duplicate the row"

        # chat_sessions.turn_count must reflect 1 logical turn, not 2.
        counter = db_conn.execute(
            "SELECT turn_count FROM chat_sessions WHERE provider_id=? AND external_session_id=?",
            ("anthropic", "ext-inflate-1"),
        ).fetchone()[0]
        assert counter == 1, (
            f"turn_count inflated by UPSERT re-fire: expected 1, got {counter}"
        )

    def test_distinct_messages_increment_normally(self, client, db_conn):
        """Two truly distinct messages must increment turn_count to 2."""
        base = {
            "session_id": "sess-upsert-2",
            "project_id": "proj_inflate2",
            "external_session_id": "ext-inflate-2",
            "provider": "openai",
            "client_source": "chat",
            "role": "assistant",
            "text": "msg",
            "char_count": 3,
        }
        client.post("/telemetry/session", json=dict(base, turn_id=1, message_id="m1"))
        client.post("/telemetry/session", json=dict(base, turn_id=2, message_id="m2"))
        counter = db_conn.execute(
            "SELECT turn_count FROM chat_sessions WHERE provider_id=? AND external_session_id=?",
            ("openai", "ext-inflate-2"),
        ).fetchone()[0]
        assert counter == 2

    def test_harness_without_message_id_increments_per_call(self, client, db_conn):
        """Harness path (no message_id) — every call is a new turn."""
        base = {
            "project_id": "proj_inflate3",
            "external_session_id": "ext-inflate-3",
            "provider": "anthropic",
            "client_source": "coding",
            "session_id": "sess-no-msgid",
        }
        client.post("/telemetry/session", json=dict(base, turn_id=1))
        client.post("/telemetry/session", json=dict(base, turn_id=2))
        client.post("/telemetry/session", json=dict(base, turn_id=3))
        counter = db_conn.execute(
            "SELECT turn_count FROM chat_sessions WHERE provider_id=? AND external_session_id=?",
            ("anthropic", "ext-inflate-3"),
        ).fetchone()[0]
        assert counter == 3


class TestBaselinePickerForInputSavings:
    """Regression: input savings formula must use the LARGEST baseline available
    (full_repo > naive_rag > no_context). Earlier code only used no_context,
    which is smaller than actual_input → savings clamped to 0 forever."""

    def _seed_pricing(self, db_conn):
        # 10 $/M input rate so 5000 saved tokens = $0.05
        db_conn.execute(
            "INSERT INTO provider_pricing (provider, model, input_per_m, output_per_m, fetched_at) "
            "VALUES (?, ?, 10.0, 50.0, '2026-06-01T00:00:00Z')",
            ("anthropic", "claude-pricing-test"),
        )
        db_conn.commit()

    def _post_injected_turn(self, client, *, baseline_no_context, baseline_full_repo=None,
                             baseline_naive_rag=None, actual_input=2500):
        return client.post("/telemetry/session", json={
            "session_id": "sess-pricing",
            "turn_id": 1,
            "project_id": "proj_pricing",
            "provider": "anthropic",
            "model": "claude-pricing-test",
            "actual_input_tokens": actual_input,
            "baseline_no_context": baseline_no_context,
            "baseline_full_repo": baseline_full_repo,
            "baseline_naive_rag": baseline_naive_rag,
            "injected": True,
            "client_source": "coding",
        })

    def test_falls_back_to_no_context_when_only_one_provided(self, client, db_conn):
        """When only baseline_no_context is provided AND it's larger than actual,
        savings should compute correctly. Mirrors browser-extension behaviour
        on cache hits (irrelevant here) and also the legacy formula path."""
        self._seed_pricing(db_conn)
        # Bypass the 7-day baseline window via env var so savings actually compute.
        import os
        os.environ["ML_DEV_BYPASS_BASELINE"] = "true"
        try:
            r = self._post_injected_turn(
                client,
                baseline_no_context=10000,
                actual_input=2500,
            )
            assert r.status_code == 200
            row = db_conn.execute(
                "SELECT saved_input_cost_usd FROM telemetry_session_timeline ORDER BY id DESC LIMIT 1"
            ).fetchone()
            # 10000 - 2500 = 7500 saved tokens × $10/M = $0.075
            assert row["saved_input_cost_usd"] == pytest.approx(0.075, abs=1e-4)
        finally:
            os.environ.pop("ML_DEV_BYPASS_BASELINE", None)

    def test_prefers_full_repo_when_present(self, client, db_conn):
        """When baseline_full_repo > others, the formula uses it as subtrahend."""
        self._seed_pricing(db_conn)
        import os
        os.environ["ML_DEV_BYPASS_BASELINE"] = "true"
        try:
            r = self._post_injected_turn(
                client,
                baseline_no_context=500,        # smallest — what old code used
                baseline_naive_rag=4000,
                baseline_full_repo=20000,       # largest — what new code uses
                actual_input=2500,
            )
            assert r.status_code == 200
            row = db_conn.execute(
                "SELECT saved_input_cost_usd FROM telemetry_session_timeline ORDER BY id DESC LIMIT 1"
            ).fetchone()
            # 20000 - 2500 = 17500 × $10/M = $0.175
            assert row["saved_input_cost_usd"] == pytest.approx(0.175, abs=1e-4)
        finally:
            os.environ.pop("ML_DEV_BYPASS_BASELINE", None)

    def test_picks_naive_rag_when_full_repo_missing(self, client, db_conn):
        self._seed_pricing(db_conn)
        import os
        os.environ["ML_DEV_BYPASS_BASELINE"] = "true"
        try:
            r = self._post_injected_turn(
                client,
                baseline_no_context=500,
                baseline_naive_rag=8000,
                actual_input=2500,
            )
            assert r.status_code == 200
            row = db_conn.execute(
                "SELECT saved_input_cost_usd FROM telemetry_session_timeline ORDER BY id DESC LIMIT 1"
            ).fetchone()
            # 8000 - 2500 = 5500 × $10/M = $0.055
            assert row["saved_input_cost_usd"] == pytest.approx(0.055, abs=1e-4)
        finally:
            os.environ.pop("ML_DEV_BYPASS_BASELINE", None)

    def test_stores_full_repo_and_naive_rag_columns(self, client, db_conn):
        """The three-baseline values must round-trip into the DB."""
        client.post("/telemetry/session", json={
            "session_id": "sess-bcols",
            "turn_id": 1,
            "project_id": "proj_bcols",
            "provider": "anthropic",
            "baseline_full_repo": 50000,
            "baseline_naive_rag": 8000,
            "baseline_no_context": 500,
        })
        row = db_conn.execute(
            "SELECT baseline_full_repo, baseline_naive_rag, baseline_no_context "
            "FROM telemetry_session_timeline ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["baseline_full_repo"] == 50000
        assert row["baseline_naive_rag"] == 8000
        assert row["baseline_no_context"] == 500


class TestOutputSavingsFormula:
    """Regression: compute_output_savings_usd unit checks + storage."""

    def test_compute_output_savings_basic(self):
        from memstrata.layer3.pricing.lookup import (
            Rates,
            compute_output_savings_usd,
        )
        r = Rates(input_per_m=10.0, output_per_m=50.0)
        # baseline_avg_output=300, actual=100 → saved=200 tokens × $50/M = $0.01
        assert compute_output_savings_usd(300, 100, r) == pytest.approx(0.01, abs=1e-6)

    def test_compute_output_savings_clamped_at_zero(self):
        from memstrata.layer3.pricing.lookup import (
            Rates,
            compute_output_savings_usd,
        )
        r = Rates(input_per_m=10.0, output_per_m=50.0)
        # actual > baseline: no savings, clamp to 0.0
        assert compute_output_savings_usd(100, 300, r) == 0.0

    @pytest.mark.requires_pro_overlay
    def test_saved_output_cost_persists_to_db(self, client, db_conn):
        """End-to-end: with a cohort baseline closed, saved_output_cost_usd
        must be populated when actual_output < cohort avg_output.

        Requires Pro overlay: the cohort_baseline table is created by
        memstrata-pro's apply_pro_schema / ProCohortApi.ensure_table.
        On Open-only the table doesn't exist and the INSERT below errors.
        """
        # Seed pricing
        db_conn.execute(
            "INSERT INTO provider_pricing (provider, model, input_per_m, output_per_m, fetched_at) "
            "VALUES (?, ?, 10.0, 50.0, '2026-06-01T00:00:00Z')",
            ("anthropic", "claude-output-test"),
        )
        # Seed a CLOSED cohort baseline with avg_output_tokens=300.
        db_conn.execute(
            "INSERT INTO cohort_baseline (project_id, baseline_started, baseline_ended, "
            "baseline_avg_turns, baseline_avg_output_tokens, active_started, last_recomputed) "
            "VALUES (?, '2026-05-01T00:00:00Z', '2026-05-08T00:00:00Z', 5.0, 300.0, "
            "'2026-05-08T00:00:00Z', '2026-05-08T00:00:00Z')",
            ("proj_output",),
        )
        db_conn.commit()

        r = client.post("/telemetry/session", json={
            "session_id": "sess-output",
            "turn_id": 1,
            "project_id": "proj_output",
            "provider": "anthropic",
            "model": "claude-output-test",
            "actual_input_tokens": 1000,
            "actual_output_tokens": 100,    # under cohort avg of 300
            "client_source": "coding",
        })
        assert r.status_code == 200
        row = db_conn.execute(
            "SELECT saved_output_cost_usd, measurement_basis FROM telemetry_session_timeline "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        # (300 - 100) × $50/M = $0.01
        assert row["saved_output_cost_usd"] == pytest.approx(0.01, abs=1e-6)
        assert row["measurement_basis"] == "output_cohort_measured"

    def test_no_cohort_means_zero_output_savings(self, client, db_conn):
        """Without a closed cohort baseline, output savings must stay 0
        (no fabrication — Hard Rule 60)."""
        db_conn.execute(
            "INSERT INTO provider_pricing (provider, model, input_per_m, output_per_m, fetched_at) "
            "VALUES (?, ?, 10.0, 50.0, '2026-06-01T00:00:00Z')",
            ("anthropic", "claude-no-cohort"),
        )
        db_conn.commit()
        import os
        os.environ["ML_DEV_BYPASS_BASELINE"] = "true"
        try:
            client.post("/telemetry/session", json={
                "session_id": "sess-no-cohort",
                "turn_id": 1,
                "project_id": "proj_no_cohort",
                "provider": "anthropic",
                "model": "claude-no-cohort",
                "actual_input_tokens": 1000,
                "actual_output_tokens": 50,
                "client_source": "coding",
            })
            row = db_conn.execute(
                "SELECT saved_output_cost_usd FROM telemetry_session_timeline "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert row["saved_output_cost_usd"] == 0.0
        finally:
            os.environ.pop("ML_DEV_BYPASS_BASELINE", None)


class TestOpenRouterSyncKeyNames:
    """Regression: OpenRouter sync must read 'input_cache_read' / 'input_cache_write',
    not the older 'cache_read' / 'image_generation' keys."""

    def test_parser_reads_input_cache_read(self):
        from memstrata.layer3.pricing.openrouter_sync import _parse_openrouter_models
        # Mimic OpenRouter's actual response shape (verified June 2026).
        rows = _parse_openrouter_models([
            {
                "id": "anthropic/claude-opus-4.8-fast",
                "pricing": {
                    "prompt": "0.00001",
                    "completion": "0.00005",
                    "input_cache_read":  "0.000001",
                    "input_cache_write": "0.0000125",
                },
            }
        ])
        assert len(rows) == 1
        r = rows[0]
        assert r["provider"] == "anthropic"
        assert r["input_per_m"]       == pytest.approx(10.0)
        assert r["output_per_m"]      == pytest.approx(50.0)
        assert r["cache_read_per_m"]  == pytest.approx(1.0)
        assert r["cache_write_per_m"] == pytest.approx(12.5)

    def test_parser_ignores_legacy_keys(self):
        """The pre-fix keys ('cache_read', 'image_generation') must NOT be used."""
        from memstrata.layer3.pricing.openrouter_sync import _parse_openrouter_models
        rows = _parse_openrouter_models([
            {
                "id": "openai/gpt-test",
                "pricing": {
                    "prompt": "0.00001",
                    "completion": "0.00005",
                    "cache_read":       "999.0",   # legacy key, must be ignored
                    "image_generation": "999.0",   # legacy key, must be ignored
                },
            }
        ])
        assert rows[0]["cache_read_per_m"]  is None
        assert rows[0]["cache_write_per_m"] is None
