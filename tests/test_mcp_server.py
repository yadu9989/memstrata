"""
MCP server tests.

Two layers of coverage:
  1. Tool functions called directly — exercises SQL queries + return shapes
  2. HTTP integration — boots the FastAPI app, mounts MCP at /mcp, and runs a
     real initialize → tools/list → tools/call protocol sequence over HTTP
     to prove that `claude mcp add --transport http memstrata
     http://localhost:8000/mcp` will actually work end-to-end.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures (mirror the isolation pattern from tests/test_api_server.py)
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
def db_conn(tmp_path):
    path = tmp_path / "test_core.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _seed_turns(client: TestClient) -> None:
    """Populate one chat session + two turns + one harness turn."""
    # Chat turn (linked to chat_session via external_session_id)
    client.post("/telemetry/session", json={
        "session_id": "ses_a",
        "turn_id": 1,
        "project_id": "demo_proj",
        "external_session_id": "ext-chat-aaa",
        "provider": "anthropic",
        "client_source": "chat",
        "role": "assistant",
        "text": "TypeScript generics tutorial — first reply",
        "actual_input_tokens": 100,
        "actual_output_tokens": 200,
    })
    client.post("/telemetry/session", json={
        "session_id": "ses_a",
        "turn_id": 2,
        "project_id": "demo_proj",
        "external_session_id": "ext-chat-aaa",
        "provider": "anthropic",
        "client_source": "chat",
        "role": "assistant",
        "text": "Follow-up on conditional types in TypeScript",
        "actual_input_tokens": 120,
        "actual_output_tokens": 250,
    })
    # Harness turn (no chat session)
    client.post("/telemetry/session", json={
        "session_id": "ses_h",
        "turn_id": 1,
        "project_id": "demo_proj",
        "provider": "anthropic",
        "client_source": "coding",
        "actual_input_tokens": 500,
        "actual_output_tokens": 100,
    })


# ---------------------------------------------------------------------------
# §1 — Tool functions called directly
# ---------------------------------------------------------------------------

class TestToolFunctionsDirect:
    def test_get_context_returns_recent_turns(self, client):
        _seed_turns(client)
        from memstrata.layer3.mcp_app import tool_get_context
        result = tool_get_context(project_id="demo_proj", limit=10)
        assert result["project_id"] == "demo_proj"
        assert result["count"] >= 2
        texts = [t["text"] for t in result["turns"]]
        assert any("TypeScript generics" in t for t in texts)
        assert any("conditional types" in t for t in texts)

    def test_get_context_empty_project(self, client):
        from memstrata.layer3.mcp_app import tool_get_context
        result = tool_get_context(project_id="nonexistent", limit=10)
        assert result["count"] == 0
        assert result["turns"] == []

    def test_list_chat_sessions_returns_seeded_session(self, client):
        _seed_turns(client)
        from memstrata.layer3.mcp_app import tool_list_chat_sessions
        result = tool_list_chat_sessions(limit=20)
        assert result["count"] >= 1
        ext_ids = [s["external_session_id"] for s in result["sessions"]]
        assert "ext-chat-aaa" in ext_ids

    def test_get_chat_history_returns_dialogue(self, client):
        _seed_turns(client)
        from memstrata.layer3.mcp_app import tool_get_chat_history, tool_list_chat_sessions
        sessions = tool_list_chat_sessions(limit=20)["sessions"]
        match = next(s for s in sessions if s["external_session_id"] == "ext-chat-aaa")
        cs_id = match["chat_session_id"]

        history = tool_get_chat_history(chat_session_id=cs_id, limit=50)
        assert history["chat_session_id"] == cs_id
        assert history["provider_id"] == "anthropic"
        assert history["count"] == 2
        # Chronological order (oldest first)
        assert history["turns"][0]["turn_id"] == 1
        assert history["turns"][1]["turn_id"] == 2

    def test_get_chat_history_unknown_id_returns_error(self, client):
        from memstrata.layer3.mcp_app import tool_get_chat_history
        result = tool_get_chat_history(chat_session_id="cs_nope_xxx", limit=10)
        assert result["count"] == 0
        assert result["error"] == "chat_session_id not found"

    def test_search_memory_substring_match(self, client):
        _seed_turns(client)
        from memstrata.layer3.mcp_app import tool_search_memory
        result = tool_search_memory(query="generics", limit=10)
        assert result["count"] >= 1
        assert any("generics" in m["text"].lower() for m in result["matches"])
        # Snippet should be present and contain the match
        assert "generics" in result["matches"][0]["snippet"].lower()

    def test_search_memory_case_insensitive(self, client):
        _seed_turns(client)
        from memstrata.layer3.mcp_app import tool_search_memory
        upper = tool_search_memory(query="TYPESCRIPT", limit=10)
        lower = tool_search_memory(query="typescript", limit=10)
        assert upper["count"] == lower["count"]
        assert upper["count"] >= 2

    def test_search_memory_project_scope(self, client):
        _seed_turns(client)
        from memstrata.layer3.mcp_app import tool_search_memory
        scoped = tool_search_memory(query="generics", project_id="demo_proj", limit=10)
        unscoped = tool_search_memory(query="generics", project_id=None, limit=10)
        assert scoped["count"] == unscoped["count"]  # all rows live in demo_proj here
        # Wrong scope returns nothing
        miss = tool_search_memory(query="generics", project_id="other_proj", limit=10)
        assert miss["count"] == 0

    def test_search_memory_empty_query(self, client):
        from memstrata.layer3.mcp_app import tool_search_memory
        result = tool_search_memory(query="   ", limit=10)
        assert result["count"] == 0
        assert result["error"] == "empty_query"

    def test_get_dashboard_stats_shape(self, client):
        _seed_turns(client)
        from memstrata.layer3.mcp_app import tool_get_dashboard_stats
        result = tool_get_dashboard_stats()
        for key in (
            "sessions", "turns", "injected_turns", "cache_hit_turns",
            "injection_rate_pct", "cache_hit_rate_pct", "savings_pct",
            "total_saved_usd", "chat_saved_usd", "coding_saved_usd",
        ):
            assert key in result, f"missing key: {key}"


# ---------------------------------------------------------------------------
# §2 — HTTP integration via the streamable HTTP transport
# ---------------------------------------------------------------------------

# MCP streamable HTTP transport spec:
#   - JSON-RPC 2.0 over POST /mcp
#   - Accept header must include both application/json and text/event-stream
#   - Server replies either with JSON or an SSE-encoded stream
#
# We don't depend on the MCP client SDK here — we hand-craft the protocol
# messages so the test stays self-contained and fast.

_ACCEPT = "application/json, text/event-stream"


def _decode_response(r) -> dict:
    """Decode either a JSON body or a single-event SSE stream."""
    ctype = r.headers.get("content-type", "")
    if "application/json" in ctype:
        return r.json()
    if "text/event-stream" in ctype:
        # SSE frame: "event: message\ndata: {json}\n\n"
        for line in r.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
    raise AssertionError(f"unexpected content-type: {ctype}\nbody:\n{r.text}")


def _initialize(client: TestClient) -> tuple[dict, str | None]:
    """Run the MCP initialize handshake and return (response, session_id)."""
    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0.0.0"},
        },
    }
    r = client.post(
        "/mcp",
        json=init_req,
        headers={"Accept": _ACCEPT, "Content-Type": "application/json"},
    )
    assert r.status_code == 200, f"initialize failed: {r.status_code} {r.text}"
    data = _decode_response(r)
    assert data.get("result") is not None, f"initialize result missing: {data}"
    assert "serverInfo" in data["result"]
    return data, r.headers.get("mcp-session-id")


class TestMcpHttpProtocol:
    def test_initialize_handshake_succeeds(self, client):
        data, _ = _initialize(client)
        assert data["result"]["serverInfo"]["name"] == "memstrata"

    def test_tools_list_returns_all_five_tools(self, client):
        _, session_id = _initialize(client)
        headers = {"Accept": _ACCEPT, "Content-Type": "application/json"}
        if session_id:
            headers["mcp-session-id"] = session_id
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=headers,
        )
        assert r.status_code == 200
        data = _decode_response(r)
        tools = {t["name"] for t in data["result"]["tools"]}
        assert tools == {
            "get_context",
            "list_chat_sessions",
            "get_chat_history",
            "search_memory",
            "get_dashboard_stats",
        }

    def test_tools_call_get_context_returns_data(self, client):
        _seed_turns(client)
        _, session_id = _initialize(client)
        headers = {"Accept": _ACCEPT, "Content-Type": "application/json"}
        if session_id:
            headers["mcp-session-id"] = session_id
        r = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_context",
                    "arguments": {"project_id": "demo_proj", "limit": 10},
                },
            },
            headers=headers,
        )
        assert r.status_code == 200
        data = _decode_response(r)
        assert "result" in data, f"call failed: {data}"
        # Tool results are wrapped in content[].text or structuredContent
        result = data["result"]
        structured = result.get("structuredContent") or {}
        # Some SDK versions return text-only; parse it as JSON if needed.
        if not structured and result.get("content"):
            text = result["content"][0].get("text", "")
            try:
                structured = json.loads(text)
            except Exception:
                structured = {}
        assert structured.get("project_id") == "demo_proj"
        assert structured.get("count", 0) >= 2

    def test_tools_call_search_memory(self, client):
        _seed_turns(client)
        _, session_id = _initialize(client)
        headers = {"Accept": _ACCEPT, "Content-Type": "application/json"}
        if session_id:
            headers["mcp-session-id"] = session_id
        r = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "search_memory",
                    "arguments": {"query": "generics", "limit": 5},
                },
            },
            headers=headers,
        )
        assert r.status_code == 200
        data = _decode_response(r)
        result = data["result"]
        structured = result.get("structuredContent") or {}
        if not structured and result.get("content"):
            structured = json.loads(result["content"][0].get("text", "{}"))
        assert structured.get("count", 0) >= 1
        assert structured["matches"][0]["text"].lower().count("generics") >= 1
