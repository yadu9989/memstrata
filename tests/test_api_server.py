"""Tests for memory_layer.layer3.api_server — Phase 31.

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
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Redirect every test to its own fresh SQLite file via environment variable."""
    monkeypatch.setenv("ML_DB_PATH", str(tmp_path / "test_core.db"))


@pytest.fixture
def client(isolated_db):
    # Lazy import so the env var is already set when the module's lifespan runs
    from memory_layer.layer3.api_server import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_conn(tmp_path):
    """Direct connection to the test DB for verification queries."""
    path = tmp_path / "test_core.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    yield conn
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
# Session registration round-trip
# ---------------------------------------------------------------------------

class TestSessionRegistration:
    def test_register_and_close(self, client):
        r = client.post("/sessions", json={
            "session_id": "ses_reg001",
            "project_id": "proj_test",
            "started_at": "2026-06-03T10:00:00Z",
            "client_id": "memory-layer-pro-harness",
        })
        assert r.status_code == 200
        assert r.json()["session_id"] == "ses_reg001"
        assert r.json()["watcher_session_id"].startswith("ws_")

        r2 = client.post("/sessions/ses_reg001/close")
        assert r2.status_code == 200
        assert r2.json()["session_id"] == "ses_reg001"
        assert "closed_at" in r2.json()


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
        assert data["injected_turns"] == 2   # turn1-ant + turn1-oai
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
        assert "Memory Layer Pro" in r.text

    def test_dashboard_references_api_endpoints(self, client):
        r = client.get("/dashboard")
        assert "/api/dashboard/state" in r.text
        assert "/api/dashboard/sessions" in r.text

    def test_dashboard_has_tab_elements(self, client):
        r = client.get("/dashboard")
        assert 'id="tabs"' in r.text
        assert 'data-tab="chat"' in r.text
        assert 'data-tab="coding"' in r.text
        assert 'id="tab-coding"' in r.text

    def test_dashboard_references_plan_features_endpoint(self, client):
        r = client.get("/dashboard")
        assert "/license/plan-features" in r.text


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
