"""
MCP (Model Context Protocol) server for MemStrata.

Exposes a small set of read-only tools to MCP clients (Claude Code, Continue,
custom agents) so they can query the local MemStrata state without going
through the HTTP API surface directly.

Mounted into the main FastAPI app at /mcp via streamable HTTP transport.
Register with: `claude mcp add --transport http memstrata http://localhost:8000/mcp`

Stateless HTTP: each request is independent, no SSE session storage. This
matches how Claude Code interacts with the server (one request → one
response, no long-lived subscriptions).

Tools exposed (read-only):
  - get_context              project-scoped recent turns
  - list_chat_sessions       browse chat sessions across providers
  - get_chat_history         turns of a specific chat session
  - search_memory            substring search across recorded turns
  - get_dashboard_stats      savings/usage metrics

No write tools — ingest happens via the existing HTTP endpoints used by the
browser extension, harness, and VS Code extension.

Architecture note: a FastMCP's session_manager.run() is single-shot and can
only be entered once per FastMCP instance. To allow each FastAPI lifespan
(including each pytest TestClient context) to start fresh, we use a factory
function (create_mcp_server) rather than a module-level singleton. The
api_server uses a dispatcher pattern to keep the mount path stable while
the underlying FastMCP is rebuilt per lifespan.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from memstrata.layer3._db import get_db_path

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-request DB connection.  Tools may run on any worker thread; sharing a
# single connection across them violates sqlite3 thread safety.
# ---------------------------------------------------------------------------

def _open_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(get_db_path()), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Tool implementations as plain functions.  Each tool re-opens the DB so it
# always sees the current ML_DB_PATH (important for test isolation).
# ---------------------------------------------------------------------------

def tool_get_context(project_id: str = "default", limit: int = 20) -> dict[str, Any]:
    """
    Return the most recent MemStrata turns for a project.

    Use this to recall what the user has been working on without scanning
    every chat thread. Turns are deduped by text and ordered newest-first.
    """
    limit = max(1, min(limit, 100))
    conn = _open_conn()
    try:
        rows = conn.execute(
            """
            SELECT MIN(role) AS role, text, MAX(recorded_at) AS recorded_at
              FROM telemetry_session_timeline
             WHERE project_id = ?
               AND text IS NOT NULL
               AND text != ''
             GROUP BY text
             ORDER BY MAX(recorded_at) DESC
             LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
    finally:
        conn.close()
    turns = [
        {"role": r["role"], "text": r["text"], "recorded_at": r["recorded_at"]}
        for r in rows
    ]
    return {"project_id": project_id, "turns": turns, "count": len(turns)}


def tool_list_chat_sessions(limit: int = 20) -> dict[str, Any]:
    """
    Browse the user's chat sessions across all AI providers.

    Returns sessions in last-seen-first order with a summary of activity.
    Useful when the user asks "what have I been working on" or "find my
    chat about X" — call this first, then call get_chat_history(chat_session_id)
    for any matches.
    """
    limit = max(1, min(limit, 200))
    conn = _open_conn()
    try:
        rows = conn.execute(
            """
            SELECT
                cs.id                                         AS chat_session_id,
                cs.provider_id,
                cs.external_session_id,
                cs.title,
                cs.first_seen,
                cs.last_seen,
                COUNT(tst.id)                                 AS turn_count,
                SUM(COALESCE(tst.actual_input_tokens,  0))    AS total_input_tokens,
                SUM(COALESCE(tst.actual_output_tokens, 0))    AS total_output_tokens,
                SUM(COALESCE(tst.saved_input_cost_usd,  0.0)) AS input_saved_usd,
                SUM(COALESCE(tst.saved_cache_cost_usd,  0.0)) AS cache_saved_usd,
                SUM(COALESCE(tst.saved_output_cost_usd, 0.0)) AS output_saved_usd
              FROM chat_sessions cs
              LEFT JOIN telemetry_session_timeline tst ON tst.chat_session_id = cs.id
             GROUP BY cs.id
             ORDER BY cs.last_seen DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    sessions = [
        {
            "chat_session_id":     r["chat_session_id"],
            "provider_id":         r["provider_id"],
            "external_session_id": r["external_session_id"],
            "title":               r["title"],
            "first_seen":          r["first_seen"],
            "last_seen":           r["last_seen"],
            "turn_count":          r["turn_count"] or 0,
            "total_input_tokens":  r["total_input_tokens"] or 0,
            "total_output_tokens": r["total_output_tokens"] or 0,
            "saved_usd": round(
                float(r["input_saved_usd"] or 0.0)
                + float(r["cache_saved_usd"] or 0.0)
                + float(r["output_saved_usd"] or 0.0),
                4,
            ),
        }
        for r in rows
    ]
    return {"sessions": sessions, "count": len(sessions)}


def tool_get_chat_history(chat_session_id: str, limit: int = 50) -> dict[str, Any]:
    """
    Return the recorded turns for a specific chat session.

    Each turn is one assistant or user message captured by the browser
    extension. Turns are returned in chronological order (oldest first).
    """
    limit = max(1, min(limit, 500))
    conn = _open_conn()
    try:
        meta = conn.execute(
            "SELECT provider_id, title FROM chat_sessions WHERE id = ?",
            (chat_session_id,),
        ).fetchone()
        if meta is None:
            return {
                "chat_session_id": chat_session_id,
                "provider_id":     None,
                "title":           None,
                "turns":           [],
                "count":           0,
                "error":           "chat_session_id not found",
            }
        rows = conn.execute(
            """
            SELECT turn_id, role, text, recorded_at
              FROM telemetry_session_timeline
             WHERE chat_session_id = ?
               AND text IS NOT NULL
               AND text != ''
             ORDER BY recorded_at ASC
             LIMIT ?
            """,
            (chat_session_id, limit),
        ).fetchall()
    finally:
        conn.close()
    turns = [
        {
            "turn_id":     r["turn_id"],
            "role":        r["role"],
            "text":        r["text"],
            "recorded_at": r["recorded_at"],
        }
        for r in rows
    ]
    return {
        "chat_session_id": chat_session_id,
        "provider_id":     meta["provider_id"],
        "title":           meta["title"],
        "turns":           turns,
        "count":           len(turns),
    }


def tool_search_memory(
    query: str,
    project_id: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """
    Search recorded chat turns for matches to a query string.

    Case-insensitive substring match. Faster than the vector retrieval path
    (no embedding required); use it when the user asks "did I talk about X"
    or "find my chat where I discussed Y".
    """
    query = (query or "").strip()
    if not query:
        return {"query": query, "matches": [], "count": 0, "error": "empty_query"}
    limit = max(1, min(limit, 50))
    pattern = f"%{query}%"
    conn = _open_conn()
    try:
        if project_id:
            rows = conn.execute(
                """
                SELECT tst.chat_session_id, tst.provider, tst.role,
                       tst.text, tst.recorded_at
                  FROM telemetry_session_timeline tst
                 WHERE tst.project_id = ?
                   AND tst.text IS NOT NULL
                   AND tst.text != ''
                   AND tst.text LIKE ? COLLATE NOCASE
                 ORDER BY tst.recorded_at DESC
                 LIMIT ?
                """,
                (project_id, pattern, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT tst.chat_session_id, tst.provider, tst.role,
                       tst.text, tst.recorded_at
                  FROM telemetry_session_timeline tst
                 WHERE tst.text IS NOT NULL
                   AND tst.text != ''
                   AND tst.text LIKE ? COLLATE NOCASE
                 ORDER BY tst.recorded_at DESC
                 LIMIT ?
                """,
                (pattern, limit),
            ).fetchall()
    finally:
        conn.close()
    matches = [
        {
            "chat_session_id": r["chat_session_id"],
            "provider":        r["provider"],
            "role":            r["role"],
            "text":            r["text"],
            "recorded_at":     r["recorded_at"],
            "snippet":         _make_snippet(r["text"], query),
        }
        for r in rows
    ]
    return {"query": query, "matches": matches, "count": len(matches)}


def tool_get_dashboard_stats() -> dict[str, Any]:
    """
    Return MemStrata usage and savings metrics.

    Useful when the user asks "how much have I saved" or "what's my
    MemStrata status". Numbers cover all activity captured so far.
    """
    # Lazy import to avoid circular dependency.
    from memstrata.layer3.api_server import _compute_dashboard_state
    conn = _open_conn()
    try:
        state = _compute_dashboard_state(conn)
    finally:
        conn.close()
    return {
        "sessions":            state.get("sessions", 0),
        "turns":               state.get("turns", 0),
        "injected_turns":      state.get("injected_turns", 0),
        "cache_hit_turns":     state.get("cache_hit_turns", 0),
        "injection_rate_pct":  state.get("injection_rate_pct", 0.0),
        "cache_hit_rate_pct":  state.get("cache_hit_rate_pct", 0.0),
        "savings_pct":         state.get("savings_pct", 0.0),
        "total_saved_usd":     state.get("total_saved_usd", 0.0),
        "chat_saved_usd":      state.get("chat_saved_usd", 0.0),
        "coding_saved_usd":    state.get("coding_saved_usd", 0.0),
    }


# ---------------------------------------------------------------------------
# Factory — builds a fresh FastMCP with all tools registered.
# Called from the api_server lifespan on every startup so that each lifespan
# (including each pytest TestClient context) gets a fresh session_manager.
# ---------------------------------------------------------------------------

def create_mcp_server() -> FastMCP:
    # DNS-rebinding protection: this server only ever runs on localhost, so
    # accept loopback hostnames on any port (`:*` wildcard supported by the
    # MCP SDK) plus the Starlette TestClient synthetic host.  Without an
    # allowlist the SDK 421s any Host header it doesn't recognise — including
    # legitimate IDE/browser clients calling http://localhost:<any>/mcp.
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "localhost", "127.0.0.1", "0.0.0.0",
            "localhost:*", "127.0.0.1:*", "0.0.0.0:*",
            "testserver",  # Starlette TestClient default
        ],
        allowed_origins=[
            "http://localhost:*", "http://127.0.0.1:*", "http://0.0.0.0:*",
            "http://localhost",   "http://127.0.0.1",   "http://0.0.0.0",
        ],
    )
    mcp = FastMCP(
        name="memstrata",
        instructions=(
            "MemStrata exposes the user's locally-captured chat history "
            "and coding-session telemetry. Use it to recall what the user "
            "has discussed with AI assistants (ChatGPT, Claude, Gemini, etc.), "
            "find context already in their memory, or check usage metrics."
        ),
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
    mcp.add_tool(tool_get_context, name="get_context")
    mcp.add_tool(tool_list_chat_sessions, name="list_chat_sessions")
    mcp.add_tool(tool_get_chat_history, name="get_chat_history")
    mcp.add_tool(tool_search_memory, name="search_memory")
    mcp.add_tool(tool_get_dashboard_stats, name="get_dashboard_stats")
    return mcp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_snippet(text: str, query: str, radius: int = 60) -> str:
    """Return a short snippet of *text* centered on the first match of *query*."""
    if not text:
        return ""
    idx = text.lower().find(query.lower())
    if idx < 0:
        return text[: radius * 2].strip()
    start = max(0, idx - radius)
    end = min(len(text), idx + len(query) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"
