"""MemStrata MIT core — FastAPI server.

Started by `memstrata api` → uvicorn.run("memstrata.layer3.api_server:app").
Consumed by:
  - The browser extension (POST /telemetry/session, GET /health, GET /baseline/status)
  - The harness (GET /context/injection, POST /sessions, POST /sessions/{id}/close,
                  POST /telemetry/session)
  - New in Phase 31: GET /context/for-chat (session-scoped retrieval)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# V5.2-D Phase D.5 prerequisite — consolidated version + start-time.
#
# /health and /system/daemon-info both read these so a future version
# bump updates ONE place. Don't sprinkle hardcoded "0.5.4" strings
# around the file.
# ---------------------------------------------------------------------------

__version__ = "0.5.4"
APP_STARTED_AT = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# .env loader — reads the project-root .env without requiring python-dotenv
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    with env_path.open() as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_dotenv()

# ---------------------------------------------------------------------------
# V5.2-E E.1: Stripe setup and the /webhooks/stripe registration moved
# to ``memstrata_pro.api_overlay`` so this Open module is entirely
# blind to billing. Pro overlay mounts at daemon startup.
# ---------------------------------------------------------------------------

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from memstrata.layer3 import retrieval as _retrieval
from memstrata.layer3._db import (
    enqueue_for_embedding,
    get_conn,
    get_db_path,
    init_db,
    is_valid_external_session_id,
    new_id,
    parse_recorded_at,
    upsert_chat_session,
)
from memstrata.layer3.pricing.lookup import (
    compute_cache_savings_usd,
    compute_input_savings_usd,
    compute_output_savings_usd,
    get_rates,
)
from memstrata.layer3.pricing.openrouter_sync import sync_loop as _pricing_sync_loop
from memstrata.workers.embedding_worker import EmbeddingWorker

# ---------------------------------------------------------------------------
# V5.2-E E.1: cohort baseline dependency injection.
#
# The cohort baseline state machine is the "money-back guarantee"
# integrity layer per Hard Rule 61 — Pro business logic. Open keeps a
# NoOp default so this module is structurally blind to baselines; Pro
# overlay (``memstrata_pro.api_overlay``) replaces it on startup.
# ---------------------------------------------------------------------------

class _NoOpCohortApi:
    """Open default — never in baseline, no stats. Pro overlay replaces."""
    def ensure_table(self, conn) -> None: pass
    def is_in_baseline_window(self, project_id: str, conn) -> bool: return False
    def days_remaining(self, project_id: str, conn): return None
    def compute_and_close_baseline(self, project_id: str, conn) -> None: pass
    def get_baseline_stats(self, project_id: str, conn): return None


_default_cohort_api: object = _NoOpCohortApi()


def _cohort_api_for_app(app_obj: FastAPI) -> object:
    return getattr(app_obj.state, "cohort_api", _default_cohort_api)


def _cohort_dep(request: Request) -> object:
    return _cohort_api_for_app(request.app)


CohortDep = Annotated[object, Depends(_cohort_dep)]



# ---------------------------------------------------------------------------
# App + lifecycle
# ---------------------------------------------------------------------------

# Phase 35: MCP server dispatcher.  FastMCP's session_manager.run() is
# single-shot per instance, so we can't reuse one across multiple lifespan
# cycles (including each pytest TestClient context).  The dispatcher keeps
# the /mcp mount path stable while the underlying FastMCP is rebuilt on
# every lifespan startup.
#
# Two transports are exposed under /mcp:
#   Streamable HTTP (--transport http):  POST /mcp
#   SSE            (--transport sse):    GET  /mcp/sse  +  POST /mcp/messages/
class _McpDispatcher:
    """ASGI app that routes between FastMCP transports mounted at /mcp.

    Path routing (after FastAPI strips the /mcp prefix):
      /sse or /messages* → SSE app   (stateful, long-lived GET + POST)
      everything else    → HTTP app  (stateless POST /)
    """

    def __init__(self) -> None:
        self._http_app = None
        self._sse_app = None

    def set_apps(self, http_app, sse_app) -> None:
        self._http_app = http_app
        self._sse_app = sse_app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "lifespan"):
            return

        if self._http_app is None:
            if scope["type"] == "http":
                await send({
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"text/plain")],
                })
                await send({"type": "http.response.body", "body": b"MCP not initialized"})
            return

        path = scope.get("path", "/")
        if self._sse_app is not None and (path == "/sse" or path.startswith("/messages")):
            await self._sse_app(scope, receive, send)
        else:
            await self._http_app(scope, receive, send)


_mcp_dispatcher = _McpDispatcher()


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = sqlite3.connect(str(get_db_path()), check_same_thread=False)
    try:
        init_db(conn)
        # V5.2-E E.1 — cohort_baseline table creation moved behind the
        # Pro overlay's cohort_api. Open's default is a no-op so the
        # table is simply absent when the daemon runs without Pro.
        _cohort_api_for_app(app).ensure_table(conn)
    finally:
        conn.close()

    def _conn_factory():
        c = sqlite3.connect(str(get_db_path()), check_same_thread=False, timeout=10.0)
        c.row_factory = sqlite3.Row
        return c

    import asyncio
    # V5.2-E CI fix: the OpenRouter pricing sync runs the network fetch +
    # SQLite write inside ``asyncio.to_thread(...)``. Threads spawned by
    # to_thread() cannot be cancelled mid-execution. In tests, every
    # TestClient enters/exits the lifespan; threads from prior
    # invocations are still mid-INSERT against the same SQLite path when
    # the next test's setup closes the connection — race condition,
    # segfault during fixture teardown (seen on Ubuntu cp310 and Windows
    # cp310/cp311). Skip the background sync when
    # MEMSTRATA_DISABLE_PRICING_SYNC=1 (set in tests/conftest.py). The
    # daemon still has the static pricing_matrix.json fallback, so
    # tests get pricing data; live OpenRouter rates only matter for the
    # dashboard's savings calculator in production.
    if os.environ.get("MEMSTRATA_DISABLE_PRICING_SYNC") == "1":
        task = None
    else:
        task = asyncio.create_task(_pricing_sync_loop(_conn_factory))

    # V5.2-C Phase C.2 — Ollama health polling task.
    # Initializes app.state.ollama_status to UNKNOWN, then polls
    # localhost:11434 every 30 s while non-READY; once READY, falls
    # back to a 5-minute heartbeat so we still notice if Ollama dies.
    # Hard Rule 80: this MUST NOT block startup. The task is fired
    # and forgotten; the lifespan continues immediately.
    from memstrata.layer3.ollama_health import OllamaHealth, OllamaStatus
    app.state.ollama_status = OllamaHealth(
        status=OllamaStatus.UNKNOWN,
        configured_model="",
    )
    app.state.ollama_last_checked_at = None
    ollama_task = asyncio.create_task(_ollama_polling_loop(app))

    worker = EmbeddingWorker()
    worker.start()

    # V5.2-A Phase 35.9 — start the codebase ingestion service.
    # Initializes ResourcePolicy, runs branch-switch sweep per opted-in
    # project, starts CodebaseWatcher per project. Failures during
    # startup are logged but never abort the FastAPI lifespan — the
    # MIT core must keep serving even when ingestion can't initialize
    # (e.g. watchdog unavailable, missing project_opt_in rows).
    ingestion_service = None
    try:
        from memstrata.layer3.ingestion import IngestionService
        # ML_INGESTION_DISABLED=1 lets operators kill the watchers
        # without uninstalling the package (handy for diagnostics).
        if os.environ.get("ML_INGESTION_DISABLED") != "1":
            ingestion_service = IngestionService(
                db_path=str(get_db_path()),
                # autostart_watchers respects ML_INGESTION_WATCH=0 so the
                # branch-switch sweep can run alone during initial dev.
                autostart_watchers=os.environ.get("ML_INGESTION_WATCH", "1") != "0",
                autostart_sweeps=os.environ.get("ML_INGESTION_SWEEP", "1") != "0",
            )
            ingestion_service.start()
            app.state.ingestion_service = ingestion_service
            _logger.info(
                "ingestion: started, %d project(s) opted in",
                len(ingestion_service.projects()),
            )
    except Exception as exc:                              # noqa: BLE001
        _logger.warning("ingestion: startup failed: %s", exc)
        ingestion_service = None

    # Build a fresh FastMCP for this lifespan and wire it to the dispatcher.
    # Both transports are exposed: streamable HTTP (POST /) and SSE (GET /sse).
    # Register with:
    #   --transport http: claude mcp add --transport http memstrata http://localhost:8000/mcp
    #   --transport sse:  claude mcp add --transport sse  memstrata http://localhost:8000/mcp
    from memstrata.layer3.mcp_app import create_mcp_server
    fresh_mcp = create_mcp_server()
    _mcp_dispatcher.set_apps(
        fresh_mcp.streamable_http_app(),
        fresh_mcp.sse_app(),
    )

    async with fresh_mcp.session_manager.run():
        try:
            yield
        finally:
            # V5.2-A Phase 35.9 — shut down the ingestion service first
            # so any in-flight reindex completes before we close DB
            # connections. stop() joins the sweep + watcher threads.
            if ingestion_service is not None:
                try:
                    ingestion_service.stop()
                except Exception as exc:                  # noqa: BLE001
                    _logger.debug("ingestion: shutdown raised: %s", exc)
            worker.stop()
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            # V5.2-C Phase C.2 — cancel the Ollama polling task last
            # (cheap; just a sleep loop). Mirrors the pricing-sync
            # shutdown pattern above.
            ollama_task.cancel()
            try:
                await ollama_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="MemStrata Core", version="0.5.4", lifespan=lifespan)

# Mount the dispatcher at /mcp.  Register the server with:
#   claude mcp add --transport http memstrata http://localhost:8000/mcp
app.mount("/mcp", _mcp_dispatcher)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


# V5.2-E E.1: Stripe webhook registration moved to
# ``memstrata_pro.api_overlay._register_stripe_webhook``.


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "alive", "version": __version__}


# ---------------------------------------------------------------------------
# V5.2-C Phase C.2 — Ollama health
# ---------------------------------------------------------------------------
#
# Hard Rule 80: this endpoint always responds; the daemon never blocks
# on Ollama. Dashboard polls this to render the top banner; wizard
# screens 2 + 3 read it to decide which step to surface.

_OLLAMA_POLL_INTERVAL_NON_READY_S = 30.0
_OLLAMA_POLL_INTERVAL_READY_S = 300.0     # 5-min heartbeat once READY


async def _ollama_polling_loop(app_ref) -> None:
    """Continuously refresh app.state.ollama_status.

    Polls every 30 s while non-READY (matches the addendum's "user just
    finished installing Ollama" case) and every 5 min once READY (cheap
    heartbeat so a crashed Ollama still gets noticed without waiting
    for the next user-initiated request).

    Catches every exception — Hard Rule 80: the loop never dies.
    """
    import asyncio
    from datetime import datetime, timezone

    from memstrata.layer3.ollama_health import (
        OllamaStatus,
        check_ollama_async,
    )

    while True:
        try:
            health = await check_ollama_async()
            app_ref.state.ollama_status = health
            app_ref.state.ollama_last_checked_at = datetime.now(
                timezone.utc
            ).isoformat()
            interval = (
                _OLLAMA_POLL_INTERVAL_READY_S
                if health.status == OllamaStatus.READY
                else _OLLAMA_POLL_INTERVAL_NON_READY_S
            )
        except Exception as exc:                              # noqa: BLE001
            _logger.warning("ollama polling iteration failed: %s", exc)
            interval = _OLLAMA_POLL_INTERVAL_NON_READY_S
        await asyncio.sleep(interval)


@app.get("/system/ollama-status")
def ollama_status() -> dict:
    """Current Ollama health, refreshed by the background poller.

    Returns ``{"status": "unknown", ...}`` until the first poll lands
    — typically within a few hundred ms of startup.
    """
    health = getattr(app.state, "ollama_status", None)
    last_checked = getattr(app.state, "ollama_last_checked_at", None)
    if health is None:
        return {
            "status": "unknown",
            "configured_model": "",
            "installed_models": [],
            "error_detail": None,
            "last_checked_at": None,
        }
    return {
        "status": health.status.value,
        "configured_model": health.configured_model,
        "installed_models": list(health.installed_models),
        "error_detail": health.error_detail,
        "last_checked_at": last_checked,
    }


# ---------------------------------------------------------------------------
# V5.2-D Phase D.5 — daemon info + shutdown endpoints
# ---------------------------------------------------------------------------
#
# Consumed by the memstrata-pro-tray process (V5.2-D §4.6). Both are
# 127.0.0.1-only by virtue of the uvicorn host binding done in
# memstrata/cli/main.py — no additional middleware needed.


@app.get("/system/daemon-info")
def daemon_info() -> dict:
    """Used by the tray to display version + uptime in its menu."""
    now = datetime.now(timezone.utc)
    return {
        "version": __version__,
        "started_at": APP_STARTED_AT.isoformat(),
        "pid": os.getpid(),
        "uptime_seconds": (now - APP_STARTED_AT).total_seconds(),
    }


class ShutdownRequest(BaseModel):
    """Body for POST /system/shutdown.

    Hard Rule 84: ``confirmed`` must be True. The tray UI shows a native
    confirmation dialog before sending this, AND the server checks again
    here as defense in depth (so a buggy tray, a curl typo, or future
    automation can't drop the daemon by accident).
    """
    source: str = "unknown"
    confirmed: bool = False


@app.post("/system/shutdown", status_code=202)
async def shutdown_daemon(request: ShutdownRequest) -> dict:
    """Graceful shutdown initiated by the tray.

    Returns 202 (Accepted) immediately, then schedules a SIGINT to self
    after 0.5 s so the HTTP response completes first. The existing
    lifespan cleanup handles graceful teardown of the embedding worker,
    pricing sync, ingestion service, and Ollama polling task.
    """
    if not request.confirmed:
        raise HTTPException(
            status_code=400,
            detail="shutdown requires confirmed=true",
        )
    _logger.info(
        "shutdown requested via API (source=%s, pid=%d)",
        request.source, os.getpid(),
    )
    import asyncio
    loop = asyncio.get_event_loop()
    loop.call_later(0.5, _trigger_shutdown)
    return {"status": "shutdown_scheduled"}


def _trigger_shutdown() -> None:
    """Send the platform-appropriate graceful-shutdown signal to self.

    SIGINT works on both POSIX and Windows; uvicorn catches it the same
    way it catches Ctrl-C in foreground mode, runs lifespan cleanup,
    and exits. Falls back to ``os._exit(0)`` only if signaling fails —
    in that case lifespan cleanup may not complete, but the process
    still terminates so the tray sees the daemon go away.
    """
    import signal
    try:
        signal.raise_signal(signal.SIGINT)
    except Exception as exc:                          # noqa: BLE001
        _logger.error("graceful shutdown signal failed: %s; falling back to os._exit", exc)
        os._exit(0)


# ---------------------------------------------------------------------------
# Session registration / close  (called by the harness)
# ---------------------------------------------------------------------------

class RegisterSessionBody(BaseModel):
    session_id: str
    project_id: str
    started_at: str
    client_id: str | None = None
    context_hash: str | None = None


@app.post("/sessions")
def register_session(body: RegisterSessionBody, conn: Conn) -> dict:
    conn.execute(
        """
        INSERT OR IGNORE INTO sessions (id, project_id, started_at, client_id, context_hash)
        VALUES (?, ?, ?, ?, ?)
        """,
        (body.session_id, body.project_id, body.started_at, body.client_id, body.context_hash),
    )
    conn.commit()
    return {"session_id": body.session_id, "watcher_session_id": new_id("ws")}


@app.post("/sessions/{session_id}/close")
def close_session(session_id: str, conn: Conn) -> dict:
    closed_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE sessions SET closed_at = ? WHERE id = ?",
        (closed_at, session_id),
    )
    conn.commit()
    return {"session_id": session_id, "closed_at": closed_at}


# ---------------------------------------------------------------------------
# Project registration — POST /projects/register
#
# Reactivates the dormant VS Code auto-ingestion flow per V5.2-A §6.1. The
# Pro extension calls this on activation with the open workspace folder so
# the daemon-side IngestionService (Hard Rule 70 / 73) can begin watching
# the project without the user manually running `memstrata ingest`.
#
# The route is intentionally Open-side: the browser extension's tier probe
# and any future first-class IDE clients (Cursor, Zed, JetBrains) all need
# the same opt-in handshake, and putting it in the Pro overlay would gate
# free-tier users out of the watcher entirely.
# ---------------------------------------------------------------------------

class RegisterProjectBody(BaseModel):
    path: str
    # Optional override for the canonical project_id used in code_chunks /
    # codebase_files. When omitted, the IngestionService derives it from
    # the path. The Pro VS Code extension passes its workspace name here
    # so the harness's `x-project-id` header (set to vscode.workspace.name)
    # finds the ingested rows on the /context/injection lookup.
    project_id: str | None = None
    user_added_dirs: list[str] | None = None
    user_excluded_dirs: list[str] | None = None


@app.post("/projects/register")
def register_project_route(body: RegisterProjectBody, conn: Conn, request: Request) -> dict:
    """Opt a project in for IngestionService watching.

    The Pro VS Code extension calls this on activation with the open
    workspace folder. The route:
      1. Writes / upserts the ``project_opt_in`` row (Hard Rule 70 gate).
      2. If the IngestionService is live, hands the path to ``add_project``
         so watching starts immediately — no daemon restart required.

    Returns the resolved absolute path AND the canonical project_id so
    the caller can mirror it into its own x-project-id header values.
    """
    from memstrata.layer3.ingestion.orchestrator import record_opt_in

    raw = Path(body.path).expanduser()
    if not raw.exists():
        raise HTTPException(status_code=400, detail=f"path does not exist: {body.path}")
    if not raw.is_dir():
        raise HTTPException(status_code=400, detail=f"not a directory: {body.path}")
    resolved = str(raw.resolve())

    record_opt_in(
        conn,
        resolved,
        user_added_dirs=body.user_added_dirs,
        user_excluded_dirs=body.user_excluded_dirs,
    )

    # Best-effort live attach. NotOptedIn can't fire here because we just
    # wrote the row, but watchdog construction or sweep startup can still
    # raise on platform-specific surfaces (e.g., inotify limit). The route
    # stays successful in that case — the watcher will pick up the project
    # at the next daemon restart from project_opt_in regardless.
    watcher_started = False
    effective_project_id: str | None = body.project_id
    service = getattr(request.app.state, "ingestion_service", None)
    if service is not None:
        try:
            runtime = service.add_project(resolved, project_id=body.project_id)
            watcher_started = True
            effective_project_id = runtime.project_id
        except Exception as exc:                                # noqa: BLE001
            _logger.warning(
                "register_project: live attach failed for %s: %s",
                resolved, exc,
            )

    return {
        "project_path": resolved,
        "project_id": effective_project_id,
        "state": "opted_in",
        "watcher_started": watcher_started,
    }


# ---------------------------------------------------------------------------
# Telemetry ingest — POST /telemetry/session
# Phase 31: when external_session_id is present, upsert chat_sessions and set
# the FK on the telemetry row.
# ---------------------------------------------------------------------------

class TurnTelemetryBody(BaseModel):
    session_id: str
    turn_id: int
    project_id: str
    # V5.4 Phase 30: browser extension attaches the provider's own URL session ID
    external_session_id: str | None = None
    # Stream-pause dedup key: stable DOM-node identifier generated by the extension.
    # When present, the backend UPSERTs on (session_id, message_id) so a paused
    # model that fires onComplete twice updates the row instead of inserting a dupe.
    message_id: str | None = None
    # Explicit client origin for dashboard Chat vs Coding split.
    # Canonical values: 'chat' (browser extension) | 'coding' (harness/VS Code).
    # Legacy values 'browser_ext'/'harness' still accepted for backward compat.
    # When omitted by an older caller (typically the harness), the route handler
    # defaults this to 'coding' — see record_turn() below.
    client_source: str | None = None
    # Browser extension captured turn content
    role: str | None = None
    text: str | None = None
    char_count: int | None = None
    # Harness financial telemetry fields
    provider: str | None = None
    model: str | None = None
    actual_input_tokens: int | None = None
    actual_output_tokens: int | None = None
    baseline_full_repo: int | None = None
    baseline_naive_rag: int | None = None
    baseline_no_context: int | None = None
    tokens_estimated: bool = False
    block_hash: str | None = None
    injected: bool = False
    latency_ms_total: int | None = None
    latency_ms_ttft: int | None = None
    cache_eligible: bool = False
    cache_hit_estimated: bool = False
    incomplete: bool = False


@app.post("/telemetry/session")
def record_turn(body: TurnTelemetryBody, conn: Conn, cohort: CohortDep) -> dict:
    chat_session_id: str | None = None

    # Normalize client_source to canonical 'chat' | 'coding' so the dashboard
    # split never has to handle legacy variants. Older browser builds shipped
    # 'browser_ext' or 'web'; older harness builds shipped 'harness' or NULL.
    # Mapping (kept tiny so the call path stays sub-1ms):
    #   'chat' | 'browser_ext' | 'web'  → 'chat'
    #   'coding' | 'harness' | None     → 'coding'
    raw = (body.client_source or "").strip().lower()
    if raw in ("chat", "browser_ext", "web"):
        client_source = "chat"
    else:
        # 'coding', 'harness', '', None, or any unknown value defaults to coding.
        # Un-tagged callers are by definition harness/IDE telemetry historically.
        client_source = "coding"

    # Detect whether this POST is a fresh insert or a stream-pause UPSERT update.
    # Browser extension sends a stable message_id; the second fire for the same
    # message must NOT advance chat_sessions.turn_count, otherwise the dashboard
    # over-counts by 1 per re-fire (cosmetic but visible — observed in production
    # data as cs_575100038e2c429d counter=4 with only 3 actual rows).
    is_new_turn = True
    if body.message_id:
        existing = conn.execute(
            "SELECT 1 FROM telemetry_session_timeline "
            "WHERE session_id = ? AND message_id = ? LIMIT 1",
            (body.session_id, body.message_id),
        ).fetchone()
        if existing:
            is_new_turn = False

    # Phase 33 — validated upsert: strip, validate format, then link to chat session.
    # On any failure we degrade gracefully: turn is stored with chat_session_id=NULL.
    # Telemetry must never return an error to the caller.
    if body.external_session_id and body.provider:
        ext_id = body.external_session_id.strip()
        provider = body.provider.strip()
        if ext_id and provider and is_valid_external_session_id(ext_id):
            try:
                chat_session_id = upsert_chat_session(
                    conn, provider, ext_id,
                    increment_turn_count=is_new_turn,
                )
            except Exception as exc:
                _logger.warning(
                    "upsert_chat_session failed (provider=%r ext_id=%r): %s — "
                    "storing turn without session link",
                    provider, ext_id, exc,
                )
        else:
            _logger.debug(
                "Skipping invalid external_session_id=%r (provider=%r)",
                body.external_session_id, body.provider,
            )

    # Phase 21 — financial telemetry: compute dollar savings when rates are known.
    saved_input_usd: float = 0.0
    saved_cache_usd: float = 0.0
    saved_output_usd: float = 0.0
    measurement_basis: str = "input_measured"
    in_baseline = False

    if body.project_id:
        try:
            in_baseline = cohort.is_in_baseline_window(body.project_id, conn)
            if not in_baseline:
                cohort.compute_and_close_baseline(body.project_id, conn)
        except Exception as exc:
            _logger.debug("baseline check failed for %s: %s", body.project_id, exc)

    # The savings subtrahend MUST be the largest "naive" baseline the caller
    # was able to compute — i.e., what they would have spent without Memory
    # Layer. Earlier code always used baseline_no_context (prompt only) which
    # is SMALLER than actual_input (prompt + injected context); the formula
    # then returned max(0, smallest - larger) = 0 forever.
    #
    # Resolution order: full_repo > naive_rag > no_context. Whichever the
    # caller managed to produce becomes the comparison baseline.
    _baseline_for_input = (
        body.baseline_full_repo
        if body.baseline_full_repo is not None
        else body.baseline_naive_rag
        if body.baseline_naive_rag is not None
        else body.baseline_no_context
    )

    if (
        not in_baseline
        and body.provider
        and body.model
        and body.actual_input_tokens is not None
    ):
        rates = get_rates(body.provider, body.model, conn=conn)
        if rates is not None:
            if _baseline_for_input is not None and body.injected:
                saved_input_usd = compute_input_savings_usd(
                    _baseline_for_input,
                    body.actual_input_tokens,
                    rates,
                )
            if body.cache_hit_estimated and body.actual_input_tokens:
                saved_cache_usd = compute_cache_savings_usd(
                    body.actual_input_tokens,
                    rates,
                )
                if saved_cache_usd > 0:
                    measurement_basis = "cache_measured"
            # Output savings: per-turn delta against the cohort-measured average
            # output. Cohort stats only exist after the baseline window closes
            # (typically day 7), so this is 0 by design during the baseline
            # window and for any project that never collected a cohort.
            if body.actual_output_tokens is not None:
                try:
                    stats = cohort.get_baseline_stats(body.project_id, conn)
                except Exception:
                    stats = None
                if stats is not None:
                    _, avg_output = stats
                    saved_output_usd = compute_output_savings_usd(
                        int(round(avg_output)),
                        body.actual_output_tokens,
                        rates,
                    )
                    if saved_output_usd > 0:
                        measurement_basis = "output_cohort_measured"

    # When the extension supplies a message_id, use ON CONFLICT DO UPDATE so that
    # a mid-stream pause (debounce fires early, model resumes) updates the existing
    # row with the final text rather than inserting a duplicate.
    # When message_id is NULL (harness calls), no unique constraint applies and
    # the regular INSERT proceeds unchanged for backward compatibility.
    upsert_clause = ""
    if body.message_id:
        upsert_clause = """
        ON CONFLICT (session_id, message_id) DO UPDATE SET
            text                  = excluded.text,
            char_count            = excluded.char_count,
            recorded_at           = datetime('now'),
            actual_input_tokens   = excluded.actual_input_tokens,
            actual_output_tokens  = excluded.actual_output_tokens,
            saved_input_cost_usd  = excluded.saved_input_cost_usd,
            saved_cache_cost_usd  = excluded.saved_cache_cost_usd,
            saved_output_cost_usd = excluded.saved_output_cost_usd,
            baseline_full_repo    = COALESCE(excluded.baseline_full_repo,
                                             telemetry_session_timeline.baseline_full_repo),
            baseline_naive_rag    = COALESCE(excluded.baseline_naive_rag,
                                             telemetry_session_timeline.baseline_naive_rag),
            measurement_basis     = excluded.measurement_basis,
            baseline_period       = excluded.baseline_period,
            client_source         = COALESCE(excluded.client_source,
                                             telemetry_session_timeline.client_source),
            chat_session_id       = COALESCE(excluded.chat_session_id,
                                             telemetry_session_timeline.chat_session_id)
        """

    cur = conn.execute(
        f"""
        INSERT INTO telemetry_session_timeline (
            session_id, message_id, client_source, turn_id, project_id,
            provider, model,
            actual_input_tokens, actual_output_tokens,
            chat_session_id,
            role, text, char_count,
            baseline_no_context, baseline_full_repo, baseline_naive_rag,
            injected, cache_hit_estimated,
            saved_input_cost_usd, saved_cache_cost_usd, saved_output_cost_usd,
            measurement_basis, baseline_period
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        {upsert_clause}
        """,
        (
            body.session_id, body.message_id, client_source, body.turn_id, body.project_id,
            body.provider, body.model,
            body.actual_input_tokens, body.actual_output_tokens,
            chat_session_id,
            body.role, body.text, body.char_count,
            body.baseline_no_context,
            body.baseline_full_repo,
            body.baseline_naive_rag,
            1 if body.injected else 0,
            1 if body.cache_hit_estimated else 0,
            round(saved_input_usd, 6),
            round(saved_cache_usd, 6),
            round(saved_output_usd, 6),
            measurement_basis,
            1 if in_baseline else 0,
        ),
    )
    conn.commit()

    # Phase 34: enqueue for embedding (Hard Rule 69 — non-blocking, sub-1ms).
    # INSERT OR IGNORE: safe for both fresh inserts and UPSERT updates.
    if cur.lastrowid:
        try:
            enqueue_for_embedding(conn, cur.lastrowid)
        except Exception as exc:
            _logger.debug("enqueue_for_embedding failed for id=%d: %s", cur.lastrowid, exc)

    return {"id": body.session_id, "received_at": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# GET /context — hierarchical per-provider context retrieval (V5.4 §2.1)
#
# Called by the browser extension on every prompt-textarea input event and on
# button click. Returns recent deduped chat history scoped to the active web
# chat thread.
#
# Modes:
#   - SESSION  (external_session_id + provider both present):
#       Joins telemetry → chat_sessions on (provider_id, external_session_id)
#       and filters to client_source='browser_ext'. Strict per-thread isolation:
#       a turn ingested in ChatGPT thread A is never returned for Claude thread B.
#   - PROJECT  (external_session_id absent — harness / legacy callers):
#       Returns project-scoped turns from harness ingestion (client_source IN
#       ('harness', NULL)). Browser-extension chat content is excluded so a
#       harness context fetch never picks up another window's chat history.
#
# Guarantees:
#   - Always 200 OK, never 404 (even for brand new chat threads).
#   - token_count == 0 and text == "" when no history exists in scope.
#   - Text rows are deduped: identical text appears at most once.
# ---------------------------------------------------------------------------

@app.get("/context")
def get_context(
    conn: Conn,
    project_id: str = Query(default="default"),
    external_session_id: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """Return deduped chat history for the requesting context.

    See module-level docstring for SESSION vs PROJECT scope semantics.
    """
    rows: list = []
    scope = "project"

    if external_session_id and provider:
        scope = "session"
        ext_id = external_session_id.strip()
        prov = provider.strip()
        if ext_id and prov and is_valid_external_session_id(ext_id):
            rows = conn.execute(
                """
                SELECT MIN(tst.role) AS role,
                       tst.text,
                       MAX(tst.recorded_at) AS recorded_at
                  FROM telemetry_session_timeline tst
                  JOIN chat_sessions cs ON cs.id = tst.chat_session_id
                 WHERE cs.provider_id         = ?
                   AND cs.external_session_id = ?
                   AND tst.client_source      IN ('chat', 'browser_ext')
                   AND tst.text IS NOT NULL
                   AND tst.text != ''
                 GROUP BY tst.text
                 ORDER BY MAX(tst.recorded_at) DESC
                 LIMIT ?
                """,
                (prov, ext_id, limit),
            ).fetchall()
        else:
            _logger.debug(
                "Skipping invalid external_session_id=%r provider=%r in /context",
                external_session_id, provider,
            )
    else:
        rows = conn.execute(
            """
            SELECT MIN(role) AS role, text, MAX(recorded_at) AS recorded_at
              FROM telemetry_session_timeline
             WHERE project_id = ?
               AND (client_source = 'coding' OR client_source IS NULL)
               AND text IS NOT NULL
               AND text != ''
             GROUP BY text
             ORDER BY MAX(recorded_at) DESC
             LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()

    if not rows:
        return {
            "text":                "",
            "token_count":         0,
            "project_id":          project_id,
            "external_session_id": external_session_id,
            "scope":               scope,
        }

    # Reverse to oldest-first so the injected block reads chronologically.
    context_text = "\n\n".join(
        f"[{(r['role'] or 'assistant').upper()}] {r['text']}"
        for r in reversed(rows)
    )

    # Approximate token count (4 chars ≈ 1 token).
    token_count = max(1, len(context_text) // 4)

    return {
        "text":                context_text,
        "token_count":         token_count,
        "project_id":          project_id,
        "external_session_id": external_session_id,
        "scope":               scope,
    }


# ---------------------------------------------------------------------------
# Context injection - GET /context/injection  (called by the harness)
#
# Phase 36: now backed by the codebase_chunks table populated via
# `memstrata ingest`. Returns a stable per-project "architecture pack" block
# (README + docs + key source files) concatenated to a soft token budget.
# Stability is important - the block text + hash must NOT vary between turns
# in the same session, otherwise the harness's prefix-cache logic invalidates
# and the user pays full input cost every turn. We hash the (project_id +
# sha1s of the included files) so the hash only changes when the ingested
# content does.
#
# When the codebase has never been ingested for this project_id the endpoint
# falls back to the V5.1 empty-stub behavior so the harness still works.
# ---------------------------------------------------------------------------

_INJECTION_TOKEN_BUDGET     = 4000   # ~16 KB of code, fits comfortably in any cache
_INJECTION_DOCS_BUDGET      = 1500   # reserved for README / CLAUDE.md / *.md / *.rst
_INJECTION_PER_FILE_BUDGET  = 1000   # cap any single file's contribution so the
                                     # block holds several files, not just one big one


def _build_injection_block(
    conn: sqlite3.Connection,
    project_id: str,
) -> dict:
    """Build a stable architecture-pack block for the given project_id.

    Returns the harness's expected dict shape. When the project has no
    ingested files, returns the empty-stub shape (preserves V5.1 behavior).
    """
    total_row = conn.execute(
        """
        SELECT COUNT(*) AS n_files, COALESCE(SUM(token_count), 0) AS n_tokens
          FROM codebase_files WHERE project_id = ?
        """,
        (project_id,),
    ).fetchone()
    n_files = int(total_row["n_files"] or 0)
    raw_codebase_tokens = int(total_row["n_tokens"] or 0)

    if n_files == 0:
        return {
            "block_text": "",
            "block_hash": "empty",
            "block_built_at": datetime.now(timezone.utc).isoformat(),
            "token_count": 0,
            "expiry_hint_s": 60,
            "raw_codebase_tokens": None,
        }

    # Pick docs first (.md/.rst/.txt under top-level + docs/), then the largest
    # remaining source files. Stable order = deterministic block = stable hash.
    docs_rows = conn.execute(
        """
        SELECT path, sha1, token_count
          FROM codebase_files
         WHERE project_id = ?
           AND (lower(path) LIKE '%.md'
             OR lower(path) LIKE '%.rst'
             OR lower(path) LIKE '%.txt')
         ORDER BY token_count DESC, path ASC
         LIMIT 20
        """,
        (project_id,),
    ).fetchall()

    src_rows = conn.execute(
        """
        SELECT path, sha1, token_count
          FROM codebase_files
         WHERE project_id = ?
           AND NOT (lower(path) LIKE '%.md'
                 OR lower(path) LIKE '%.rst'
                 OR lower(path) LIKE '%.txt')
         ORDER BY token_count DESC, path ASC
         LIMIT 40
        """,
        (project_id,),
    ).fetchall()

    # Each file contributes at most _INJECTION_PER_FILE_BUDGET tokens to the
    # block; we'll later read only the first N chunks of that file. This keeps
    # any single sprawling file from eating the whole block.
    selected: list[tuple[str, str, int]] = []  # (path, sha1, effective_token_count)
    docs_used = 0
    total_used = 0
    for r in docs_rows:
        eff = min(int(r["token_count"]), _INJECTION_PER_FILE_BUDGET)
        if docs_used + eff > _INJECTION_DOCS_BUDGET:
            continue
        if total_used + eff > _INJECTION_TOKEN_BUDGET:
            continue
        selected.append((r["path"], r["sha1"], eff))
        docs_used  += eff
        total_used += eff
    for r in src_rows:
        eff = min(int(r["token_count"]), _INJECTION_PER_FILE_BUDGET)
        if total_used + eff > _INJECTION_TOKEN_BUDGET:
            continue
        selected.append((r["path"], r["sha1"], eff))
        total_used += eff

    if not selected:
        return {
            "block_text": "",
            "block_hash": "empty",
            "block_built_at": datetime.now(timezone.utc).isoformat(),
            "token_count": 0,
            "expiry_hint_s": 60,
            "raw_codebase_tokens": raw_codebase_tokens or None,
        }

    # Concatenate the chunks of each selected file in order, each wrapped in a
    # tagged header so the model knows which file it's reading.
    parts: list[str] = [
        f"# Project context: {project_id}",
        f"# Files included: {len(selected)} of {n_files} (~{total_used} tokens)",
        "",
    ]
    for path, _sha, eff_tokens in selected:
        chunks = conn.execute(
            """
            SELECT text, token_count FROM codebase_chunks
             WHERE project_id = ? AND path = ?
             ORDER BY chunk_idx
            """,
            (project_id, path),
        ).fetchall()
        if not chunks:
            continue
        parts.append(f"<file path=\"{path}\">")
        used = 0
        truncated = False
        for ch in chunks:
            tc = int(ch["token_count"] or 1)
            # Stop once we've packed roughly the per-file budget. The first
            # chunks of a file (docstring, top-level defs) are typically the
            # most informative, so this is a good "leading-N tokens" policy.
            if used + tc > eff_tokens and used > 0:
                truncated = True
                break
            parts.append(ch["text"])
            used += tc
        if truncated:
            parts.append("# ... (file truncated to fit context budget)")
        parts.append("</file>")
        parts.append("")

    block_text = "\n".join(parts).rstrip() + "\n"
    # Hash on (project + sorted file shas) so the same on-disk content always
    # produces the same hash - this is what makes the harness's prefix-cache
    # path (Hard Rule 50 FRESH_FULL/SKIP/APPEND_DELTA) work.
    h = hashlib.sha256()
    h.update(project_id.encode("utf-8"))
    for path, sha, _tokens in sorted(selected, key=lambda x: x[0]):
        h.update(b"\n")
        h.update(path.encode("utf-8"))
        h.update(b":")
        h.update(sha.encode("utf-8"))
    block_hash = h.hexdigest()[:16]
    token_count = max(1, len(block_text) // 4)

    return {
        "block_text": block_text,
        "block_hash": block_hash,
        "block_built_at": datetime.now(timezone.utc).isoformat(),
        "token_count": token_count,
        "expiry_hint_s": 300,   # block is stable; cache it client-side for 5 min
        "raw_codebase_tokens": raw_codebase_tokens,
    }


@app.get("/context/injection")
def context_injection(
    conn: Conn,
    project_id: str = Query(default="default"),
) -> dict:
    try:
        return _build_injection_block(conn, project_id)
    except Exception as exc:
        _logger.warning("context_injection failed for %s: %s", project_id, exc)
        # Hard Rule 64: never break the caller; degrade to the V5.1 empty stub.
        return {
            "block_text": "",
            "block_hash": "empty",
            "block_built_at": datetime.now(timezone.utc).isoformat(),
            "token_count": 0,
            "expiry_hint_s": 60,
            "raw_codebase_tokens": None,
        }


# ---------------------------------------------------------------------------
# Phase 31 — Session-scoped context retrieval  GET /context/for-chat
# ---------------------------------------------------------------------------

@app.get("/context/for-chat")
def context_for_chat(
    conn: Conn,
    chat_session_id: str = Query(..., description="Internal chat_sessions.id"),
    scope: str = Query(
        default="session",
        description="'session' (default) or 'provider' to expand across all sessions for the same provider",
    ),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """Return captured chat turns scoped to a session (or provider).

    Phase 31 spec:
      - Default scope 'session': only turns whose chat_session_id == the given ID.
      - scope='provider': all turns for any session that shares the same provider_id.

    Cross-session isolation guarantee: a turn ingested under session A will never
    appear in the response for session B unless scope=provider is explicitly requested
    and both sessions share a provider.
    """
    if scope == "provider":
        provider_row = conn.execute(
            "SELECT provider_id FROM chat_sessions WHERE id = ?",
            (chat_session_id,),
        ).fetchone()
        if not provider_row:
            raise HTTPException(status_code=404, detail="chat_session_id not found")
        provider_id: str = provider_row["provider_id"]
        rows = conn.execute(
            """
            SELECT tst.turn_id, tst.role, tst.text, tst.recorded_at,
                   tst.session_id, tst.chat_session_id
              FROM telemetry_session_timeline tst
              JOIN chat_sessions cs ON cs.id = tst.chat_session_id
             WHERE cs.provider_id = ?
               AND tst.text IS NOT NULL
             ORDER BY tst.recorded_at DESC
             LIMIT ?
            """,
            (provider_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT turn_id, role, text, recorded_at, session_id, chat_session_id
              FROM telemetry_session_timeline
             WHERE chat_session_id = ?
               AND text IS NOT NULL
             ORDER BY recorded_at DESC
             LIMIT ?
            """,
            (chat_session_id, limit),
        ).fetchall()

    turns = [
        {
            "turn_id":         r["turn_id"],
            "role":            r["role"],
            "text":            r["text"],
            "recorded_at":     r["recorded_at"],
            "session_id":      r["session_id"],
            "chat_session_id": r["chat_session_id"],
        }
        for r in rows
    ]

    # Oldest-first for human readability when used as injected context
    context_text = "\n\n".join(
        f"[{(t['role'] or 'assistant').upper()}] {t['text']}"
        for t in reversed(turns)
    )

    return {
        "chat_session_id": chat_session_id,
        "scope":           scope,
        "turns":           turns,
        "text":            context_text,
        "turn_count":      len(turns),
    }


# ---------------------------------------------------------------------------
# Baseline status — GET /baseline/status  (called by the browser extension)
# ---------------------------------------------------------------------------

@app.get("/baseline/status")
def baseline_status(
    conn: Conn, cohort: CohortDep, project_id: str = Query(default="default"),
) -> dict:
    try:
        in_baseline = cohort.is_in_baseline_window(project_id, conn)
        remaining = cohort.days_remaining(project_id, conn) if in_baseline else None
    except Exception as exc:
        _logger.warning("baseline_status error for %s: %s", project_id, exc)
        in_baseline = False
        remaining = None
    return {"in_baseline": in_baseline, "days_remaining": remaining}


# ---------------------------------------------------------------------------
# Phase 32 — Dashboard: state, session list, and HTML UI
# ---------------------------------------------------------------------------

def _compute_dashboard_state(conn: sqlite3.Connection) -> dict:
    # Session/turn counts use INNER JOIN so orphaned FK rows and harness-only
    # rows (chat_session_id IS NULL) are excluded from the chat-session metrics.
    # Financial aggregates are pulled in a separate query spanning ALL turns so
    # the USD savings totals are never understated.
    row = conn.execute(
        """
        SELECT
            COUNT(DISTINCT cs.id)                        AS session_count,
            COUNT(*)                                     AS turn_count,
            SUM(COALESCE(tst.actual_input_tokens,  0))   AS total_input,
            SUM(COALESCE(tst.actual_output_tokens, 0))   AS total_output,
            SUM(COALESCE(tst.injected, 0))               AS injected_turns,
            SUM(COALESCE(tst.cache_hit_estimated,  0))   AS cache_turns
          FROM telemetry_session_timeline tst
          JOIN chat_sessions cs ON cs.id = tst.chat_session_id
        """
    ).fetchone()

    # Split financial totals by source so the dashboard can show Chat vs Coding sub-tabs.
    # Canonical values after ingest normalization (see record_turn):
    #   Chat:   client_source IN ('chat', 'browser_ext', 'web')
    #   Coding: everything else (including legacy NULL and 'harness')
    # The legacy variants are kept in the CASE expressions so historical rows
    # written before normalization shipped still bucket correctly.
    # E2E test fixtures (session_id LIKE 'e2e-%' / 'test-%') are excluded so
    # they never leak into displayed financial totals.
    fin = conn.execute(
        """
        SELECT
            SUM(COALESCE(saved_input_cost_usd,  0.0))  AS total_input_saved_usd,
            SUM(COALESCE(saved_cache_cost_usd,  0.0))  AS total_cache_saved_usd,
            SUM(COALESCE(saved_output_cost_usd, 0.0))  AS total_output_saved_usd,
            SUM(CASE WHEN client_source IN ('chat', 'browser_ext', 'web')
                     THEN COALESCE(saved_input_cost_usd,  0.0) ELSE 0.0 END) AS chat_input_saved_usd,
            SUM(CASE WHEN client_source IN ('chat', 'browser_ext', 'web')
                     THEN COALESCE(saved_cache_cost_usd,  0.0) ELSE 0.0 END) AS chat_cache_saved_usd,
            SUM(CASE WHEN client_source IN ('chat', 'browser_ext', 'web')
                     THEN COALESCE(saved_output_cost_usd, 0.0) ELSE 0.0 END) AS chat_output_saved_usd,
            SUM(CASE WHEN client_source NOT IN ('chat', 'browser_ext', 'web')
                       OR client_source IS NULL
                     THEN COALESCE(saved_input_cost_usd,  0.0) ELSE 0.0 END) AS coding_input_saved_usd,
            SUM(CASE WHEN client_source NOT IN ('chat', 'browser_ext', 'web')
                       OR client_source IS NULL
                     THEN COALESCE(saved_cache_cost_usd,  0.0) ELSE 0.0 END) AS coding_cache_saved_usd,
            SUM(CASE WHEN client_source NOT IN ('chat', 'browser_ext', 'web')
                       OR client_source IS NULL
                     THEN COALESCE(saved_output_cost_usd, 0.0) ELSE 0.0 END) AS coding_output_saved_usd
          FROM telemetry_session_timeline
         WHERE session_id NOT LIKE 'e2e-%'
           AND session_id NOT LIKE 'test-%'
        """
    ).fetchone()

    total_turns = row["turn_count"] or 0
    injected    = row["injected_turns"] or 0
    cached      = row["cache_turns"] or 0

    injection_rate = injected / total_turns if total_turns else 0.0
    cache_rate     = cached   / injected    if injected    else 0.0
    savings_pct = round(cached / total_turns * 100, 1) if total_turns else 0.0
    recall_pct  = round(injection_rate * 100, 1)

    total_input_saved_usd  = round(float(fin["total_input_saved_usd"]  or 0.0), 4)
    total_cache_saved_usd  = round(float(fin["total_cache_saved_usd"]  or 0.0), 4)
    total_output_saved_usd = round(float(fin["total_output_saved_usd"] or 0.0), 4)
    total_saved_usd        = round(total_input_saved_usd + total_cache_saved_usd + total_output_saved_usd, 4)

    chat_input_saved_usd   = round(float(fin["chat_input_saved_usd"]   or 0.0), 4)
    chat_cache_saved_usd   = round(float(fin["chat_cache_saved_usd"]   or 0.0), 4)
    chat_output_saved_usd  = round(float(fin["chat_output_saved_usd"]  or 0.0), 4)
    chat_saved_usd         = round(chat_input_saved_usd + chat_cache_saved_usd + chat_output_saved_usd, 4)

    coding_input_saved_usd  = round(float(fin["coding_input_saved_usd"]  or 0.0), 4)
    coding_cache_saved_usd  = round(float(fin["coding_cache_saved_usd"]  or 0.0), 4)
    coding_output_saved_usd = round(float(fin["coding_output_saved_usd"] or 0.0), 4)
    coding_saved_usd        = round(coding_input_saved_usd + coding_cache_saved_usd + coding_output_saved_usd, 4)

    # Token-savings estimate (legacy field kept for VS Code status bar compat).
    # Uses per-session cache-hit savings across all sessions with a chat_session_id.
    saved_row = conn.execute(
        """
        WITH per_session AS (
            SELECT CAST(
                COALESCE(
                    (
                        SUM(CASE WHEN injected=1 AND cache_hit_estimated=0
                                      AND baseline_no_context IS NOT NULL
                                 THEN CAST(actual_input_tokens - baseline_no_context AS REAL)
                                 ELSE NULL END)
                        / NULLIF(
                            SUM(CASE WHEN injected=1 AND cache_hit_estimated=0
                                          AND baseline_no_context IS NOT NULL
                                     THEN 1 ELSE 0 END),
                            0
                        )
                    ) * SUM(COALESCE(cache_hit_estimated, 0)),
                    0.0
                ) AS INTEGER
            ) AS session_saved
              FROM telemetry_session_timeline
             WHERE chat_session_id IS NOT NULL
             GROUP BY chat_session_id
        )
        SELECT COALESCE(SUM(session_saved), 0) AS total_saved
          FROM per_session
        """
    ).fetchone()
    total_tokens_saved_est = int(saved_row["total_saved"] or 0)

    return {
        "status":                   "alive",
        "sessions":                 row["session_count"] or 0,
        "turns":                    total_turns,
        "total_input_tokens":       row["total_input"]  or 0,
        "total_output_tokens":      row["total_output"] or 0,
        "injected_turns":           injected,
        "cache_hit_turns":          cached,
        "injection_rate_pct":       recall_pct,
        "cache_hit_rate_pct":       round(cache_rate * 100, 1),
        "savings_pct":              savings_pct,
        "recall_pct":               recall_pct,
        "total_tokens_saved_est":   total_tokens_saved_est,
        "total_input_saved_usd":    total_input_saved_usd,
        "total_cache_saved_usd":    total_cache_saved_usd,
        "total_output_saved_usd":   total_output_saved_usd,
        "total_saved_usd":          total_saved_usd,
        "chat_input_saved_usd":     chat_input_saved_usd,
        "chat_cache_saved_usd":     chat_cache_saved_usd,
        "chat_output_saved_usd":    chat_output_saved_usd,
        "chat_saved_usd":           chat_saved_usd,
        "coding_input_saved_usd":   coding_input_saved_usd,
        "coding_cache_saved_usd":   coding_cache_saved_usd,
        "coding_output_saved_usd":  coding_output_saved_usd,
        "coding_saved_usd":         coding_saved_usd,
    }


@app.get("/api/dashboard/state")
def dashboard_state(conn: Conn) -> dict:
    """Aggregate stats consumed by the VS Code status bar (polls every 5 s)."""
    return _compute_dashboard_state(conn)


@app.get("/api/dashboard/sessions")
def dashboard_sessions(conn: Conn) -> dict:
    """Per-thread metrics grouped by chat_session_id for the dashboard UI."""
    # Browser-captured sessions (have a chat_session_id).
    # tokens_saved_est formula:
    #   avg_context_overhead = AVG(actual_input - baseline_no_context)
    #                          over fresh-injection turns (injected=1, cache_hit=0)
    #   tokens_saved_est     = avg_context_overhead × cache_hit_turns
    # This reflects the tokens ML Pro avoided re-sending by using the KV cache.
    cs_rows = conn.execute(
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
            SUM(COALESCE(tst.injected,             0))    AS injected_turns,
            SUM(COALESCE(tst.cache_hit_estimated,  0))    AS cache_hit_turns,
            CAST(
              COALESCE(
                (
                  SUM(
                    CASE WHEN tst.injected = 1
                              AND tst.cache_hit_estimated = 0
                              AND tst.baseline_no_context IS NOT NULL
                         THEN CAST(tst.actual_input_tokens - tst.baseline_no_context AS REAL)
                         ELSE NULL END
                  )
                  / NULLIF(
                    SUM(
                      CASE WHEN tst.injected = 1
                                AND tst.cache_hit_estimated = 0
                                AND tst.baseline_no_context IS NOT NULL
                           THEN 1 ELSE 0 END
                    ), 0
                  )
                ) * SUM(COALESCE(tst.cache_hit_estimated, 0)),
                0.0
              ) AS INTEGER
            )                                             AS tokens_saved_est,
            SUM(COALESCE(tst.saved_input_cost_usd,  0.0)) AS input_saved_usd,
            SUM(COALESCE(tst.saved_cache_cost_usd,  0.0)) AS cache_saved_usd,
            SUM(COALESCE(tst.saved_output_cost_usd, 0.0)) AS output_saved_usd
          FROM chat_sessions cs
          LEFT JOIN telemetry_session_timeline tst ON tst.chat_session_id = cs.id
         GROUP BY cs.id
         ORDER BY cs.last_seen DESC
         LIMIT 200
        """
    ).fetchall()

    # Coding sessions: rows with no chat_session_id that are NOT tagged as a
    # chat origin. Everything other than 'chat'/'browser_ext'/'web' (or legacy
    # NULL on a non-chat provider) lands here. The `chat_session_id IS NULL`
    # guard prevents coding-tagged turns that happen to also have a
    # chat_session_id from double-counting against cs_rows above.
    h_rows = conn.execute(
        """
        SELECT
            session_id                                    AS external_session_id,
            provider,
            COUNT(*)                                      AS turn_count,
            SUM(COALESCE(actual_input_tokens,  0))        AS total_input_tokens,
            SUM(COALESCE(actual_output_tokens, 0))        AS total_output_tokens,
            SUM(COALESCE(injected,             0))        AS injected_turns,
            SUM(COALESCE(cache_hit_estimated,  0))        AS cache_hit_turns,
            SUM(COALESCE(saved_input_cost_usd,  0.0))     AS input_saved_usd,
            SUM(COALESCE(saved_cache_cost_usd,  0.0))     AS cache_saved_usd,
            SUM(COALESCE(saved_output_cost_usd, 0.0))     AS output_saved_usd,
            MIN(recorded_at)                              AS first_seen,
            MAX(recorded_at)                              AS last_seen
          FROM telemetry_session_timeline
         WHERE chat_session_id IS NULL
           AND session_id NOT LIKE 'e2e-%'
           AND session_id NOT LIKE 'test-%'
           AND (
                client_source IN ('coding', 'harness')
             OR (
                    client_source IS NULL
                AND (provider IS NULL OR provider NOT IN
                     ('meta', 'xai', 'copilot', 'google', 'anthropic',
                      'openai', 'perplexity', 'deepseek', 'grok', 'mistral'))
                )
           )
           AND session_id NOT IN (
               SELECT DISTINCT external_session_id
                 FROM chat_sessions
                WHERE external_session_id IS NOT NULL
           )
         GROUP BY session_id
         ORDER BY last_seen DESC
         LIMIT 50
        """
    ).fetchall()

    # Orphan chat sessions: browser-extension rows where URL session detection
    # failed (Copilot, Meta, xAI, custom deployments).  These have no chat_session_id
    # so the cs_rows JOIN above misses them entirely — without this query they'd
    # disappear from the Chat tab.  Grouped by session_id (the per-tab browser
    # extension session) since there's no external_session_id to group on.
    # Includes the legacy 'web' value emitted by older extension builds; the
    # current build emits 'chat'.
    orphan_chat_rows = conn.execute(
        """
        SELECT
            session_id                                    AS session_key,
            provider,
            COUNT(*)                                      AS turn_count,
            SUM(COALESCE(actual_input_tokens,  0))        AS total_input_tokens,
            SUM(COALESCE(actual_output_tokens, 0))        AS total_output_tokens,
            SUM(COALESCE(injected,             0))        AS injected_turns,
            SUM(COALESCE(cache_hit_estimated,  0))        AS cache_hit_turns,
            SUM(COALESCE(saved_input_cost_usd,  0.0))     AS input_saved_usd,
            SUM(COALESCE(saved_cache_cost_usd,  0.0))     AS cache_saved_usd,
            SUM(COALESCE(saved_output_cost_usd, 0.0))     AS output_saved_usd,
            MIN(recorded_at)                              AS first_seen,
            MAX(recorded_at)                              AS last_seen
          FROM telemetry_session_timeline
         WHERE chat_session_id IS NULL
           AND session_id NOT LIKE 'e2e-%'
           AND session_id NOT LIKE 'test-%'
           AND (
                client_source IN ('chat', 'browser_ext', 'web')
             OR (client_source IS NULL AND provider IN ('meta', 'xai', 'copilot', 'google'))
           )
         GROUP BY session_id, provider
         ORDER BY last_seen DESC
         LIMIT 50
        """
    ).fetchall()

    chat_sessions = [
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
            "injected_turns":      r["injected_turns"] or 0,
            "cache_hit_turns":     r["cache_hit_turns"] or 0,
            "tokens_saved_est":    r["tokens_saved_est"] or 0,
            "input_saved_usd":     round(float(r["input_saved_usd"]  or 0.0), 4),
            "cache_saved_usd":     round(float(r["cache_saved_usd"]  or 0.0), 4),
            "output_saved_usd":    round(float(r["output_saved_usd"] or 0.0), 4),
            # Explicit source tag so the frontend doesn't have to infer Chat vs
            # Coding from chat_session_id IS NULL (which incorrectly buckets
            # orphan browser-extension sessions into Coding).
            "source":              "chat",
        }
        for r in cs_rows
    ]

    harness_sessions = [
        {
            "chat_session_id":     None,
            "provider_id":         r["provider"] or "harness",
            "external_session_id": r["external_session_id"],
            "title":               None,
            "first_seen":          r["first_seen"],
            "last_seen":           r["last_seen"],
            "turn_count":          r["turn_count"] or 0,
            "total_input_tokens":  r["total_input_tokens"] or 0,
            "total_output_tokens": r["total_output_tokens"] or 0,
            "injected_turns":      r["injected_turns"] or 0,
            "cache_hit_turns":     r["cache_hit_turns"] or 0,
            "tokens_saved_est":    0,
            "input_saved_usd":     round(float(r["input_saved_usd"]  or 0.0), 4),
            "cache_saved_usd":     round(float(r["cache_saved_usd"]  or 0.0), 4),
            "output_saved_usd":    round(float(r["output_saved_usd"] or 0.0), 4),
            "source":              "coding",
        }
        for r in h_rows
    ]

    # Orphan chat sessions (web chats where session detection failed): surface them
    # in the Chat tab so Copilot/Meta/xAI captures are visible to the user.
    orphan_chat_sessions = [
        {
            "chat_session_id":     None,
            "provider_id":         r["provider"] or "unknown",
            "external_session_id": r["session_key"],
            "title":               None,
            "first_seen":          r["first_seen"],
            "last_seen":           r["last_seen"],
            "turn_count":          r["turn_count"] or 0,
            "total_input_tokens":  r["total_input_tokens"] or 0,
            "total_output_tokens": r["total_output_tokens"] or 0,
            "injected_turns":      r["injected_turns"] or 0,
            "cache_hit_turns":     r["cache_hit_turns"] or 0,
            "tokens_saved_est":    0,
            "input_saved_usd":     round(float(r["input_saved_usd"]  or 0.0), 4),
            "cache_saved_usd":     round(float(r["cache_saved_usd"]  or 0.0), 4),
            "output_saved_usd":    round(float(r["output_saved_usd"] or 0.0), 4),
            # Tagged 'chat' (not 'coding') even though chat_session_id IS NULL —
            # this is what fixes the dashboard bug where Copilot/Meta/xAI orphan
            # sessions were being lumped into the Coding tab.
            "source":              "chat",
        }
        for r in orphan_chat_rows
    ]

    all_sessions = chat_sessions + orphan_chat_sessions + harness_sessions
    return {"sessions": all_sessions, "total_count": len(all_sessions)}


_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MemStrata — Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;font-size:13px;background:#0f1117;color:#c8d0da;min-height:100vh}
header{padding:12px 20px;border-bottom:1px solid #222733;display:flex;align-items:center;gap:10px}
h1{font-size:14px;font-weight:600;color:#e8eaf0;letter-spacing:.01em}
.tier-badge{font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;border-radius:9999px;padding:2px 9px;border:1px solid}
.tier-free{background:#1a1e2c;color:#5a6478;border-color:#2a3050}
.tier-trial,.tier-pro{background:#1a2636;color:#60a5fa;border-color:#2a4060}
.tier-lite{background:#1e2630;color:#94a3b8;border-color:#334155}
.tier-team{background:#1e1a36;color:#a78bfa;border-color:#3b2f6a}
#ts{font-size:11px;color:#4e5570;margin-left:auto}
/* ── Top-level tabs ── */
#tabs{display:flex;gap:0;padding:0 20px;border-bottom:1px solid #1e222e}
.tab{padding:10px 16px;font-size:13px;font-weight:500;color:#5a6478;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;user-select:none}
.tab.active{color:#7dd3a8;border-bottom-color:#7dd3a8}
.tab-content{display:none;padding:20px}
.tab-content.active{display:block}
/* PRO_MONEY_TAB_CSS */
/* ── Now tab ── */
.now-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:20px}
.now-card{background:#161b24;border:1px solid #222733;border-radius:8px;padding:14px 16px}
.now-val{font-size:26px;font-weight:600;color:#e8eaf0}
.now-lbl{font-size:11px;color:#5a6478;margin-top:3px}
.sc{background:#13171f;border:1px solid #1e2330;border-radius:6px;padding:9px 13px;margin-bottom:6px}
.st{display:flex;align-items:center;gap:6px;margin-bottom:6px}
.sid{font-family:ui-monospace,monospace;font-size:11px;color:#8aa4c0}
.stitle{font-size:12px;color:#b8c4d4}
.sage{font-size:11px;color:#3e4a5e;margin-left:auto}
.metrics{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}
.pill{font-size:11px;border-radius:10px;padding:2px 8px}
.p-base{background:#1a1e2c;color:#7080a0}
.p-inj{background:#172519;color:#6dba8c}
.p-cache{background:#152030;color:#5a9ec8}
.p-save{background:#1e1830;color:#a07ecf}
/* ── Quality tab ── */
.qual-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:20px}
.qual-card{background:#161b24;border:1px solid #222733;border-radius:8px;padding:14px 16px}
.qual-val{font-size:26px;font-weight:600;color:#818cf8}
.qual-lbl{font-size:11px;color:#5a6478;margin-top:3px}
.qual-sub{font-size:10px;color:#3a4458;margin-top:2px}
/* ── Shared ── */
.section-title{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#4e5570;margin-bottom:12px}
.pg{margin-bottom:18px}
.ph{font-size:10px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:#5a6478;margin-bottom:7px;display:flex;align-items:center;gap:7px}
.pc{background:#1a1e2c;border-radius:10px;padding:1px 7px;font-weight:400;font-size:10px}
#err{padding:12px 20px;color:#c06a6a;font-size:12px;display:none}
</style>
</head>
<body>
<header>
  <h1>MemStrata</h1>
  <span id="tier-badge" class="tier-badge tier-free">free</span>
  <span id="ts">Loading…</span>
</header>
<div id="tabs">
  <!-- PRO_MONEY_TAB_NAV -->
  <span class="tab" data-tab="now">Now</span>
  <span class="tab" data-tab="quality">Quality</span>
</div>

<!-- PRO_MONEY_TAB_BODY -->

<!-- ── Now tab ── -->
<div id="tab-now" class="tab-content active">
  <div class="now-grid">
    <div class="now-card"><div class="now-val" id="n-sessions">0</div><div class="now-lbl">total sessions</div></div>
    <div class="now-card"><div class="now-val" id="n-turns">0</div><div class="now-lbl">total turns</div></div>
    <div class="now-card"><div class="now-val" id="n-injected">0</div><div class="now-lbl">context injections</div></div>
    <div class="now-card"><div class="now-val" id="n-cached">0</div><div class="now-lbl">cache hits</div></div>
  </div>
  <div class="section-title">Recent sessions</div>
  <div id="now-sessions"></div>
</div>

<!-- ── Quality tab ── -->
<div id="tab-quality" class="tab-content">
  <div class="qual-grid">
    <div class="qual-card"><div class="qual-val" id="q-inj-rate">0%</div><div class="qual-lbl">Injection rate</div><div class="qual-sub">turns with context injected</div></div>
    <div class="qual-card"><div class="qual-val" id="q-cache-rate">0%</div><div class="qual-lbl">Cache hit rate</div><div class="qual-sub">injected turns that hit KV cache</div></div>
    <div class="qual-card"><div class="qual-val" id="q-savings-pct">0%</div><div class="qual-lbl">Savings rate</div><div class="qual-sub">all turns that avoided re-send</div></div>
    <div class="qual-card"><div class="qual-val" id="q-tok-in">0</div><div class="qual-lbl">Total input tokens</div><div class="qual-sub">across all providers</div></div>
    <div class="qual-card"><div class="qual-val" id="q-tok-out">0</div><div class="qual-lbl">Total output tokens</div><div class="qual-sub">across all providers</div></div>
    <div class="qual-card"><div class="qual-val" id="q-tok-saved">0</div><div class="qual-lbl">Tokens saved (est.)</div><div class="qual-sub">tokens not re-sent via cache</div></div>
  </div>
</div>

<div id="err"></div>
<script>
var _state={};
var _sessions=[];
var _plan={plan:'free',features:[]};
var _activeTab=location.hash.replace(/^#\\//,'') || 'now';
/* PRO_MONEY_TAB_JS_INIT */

/* PRO_MONEY_TAB_JS_USD */
function fmt(n){if(n==null)return'–';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'K';return''+n}
// SQLite emits 'YYYY-MM-DD HH:MM:SS' UTC with no zone marker, which JS Date
// parses as LOCAL time → every row reads as future ("just now") west of UTC.
// Normalize to ISO-8601 with explicit 'Z' before parsing so Date is in UTC.
function parseTs(ts){if(!ts)return NaN;var s=String(ts);if(/[zZ]|[+-]\\d{2}:?\\d{2}$/.test(s))return new Date(s).getTime();return new Date(s.replace(' ','T')+'Z').getTime()}
function rel(ts){if(!ts)return'';var t=parseTs(ts);if(isNaN(t))return'';var d=(Date.now()-t)/1000;if(d<0)d=0;if(d<60)return'just now';if(d<3600)return~~(d/60)+'m ago';if(d<86400)return~~(d/3600)+'h ago';return~~(d/86400)+'d ago'}
function abbr(s){if(!s)return'–';return s.length>16?s.slice(0,16)+'\\u2026':s}
function hasFeat(f){return _plan.features.indexOf(f)>=0}

// ── Top-level tab routing ──────────────────────────────────────────────────
function switchTab(tab){
  _activeTab=tab;
  location.hash='/'+tab;
  document.querySelectorAll('.tab').forEach(function(t){t.classList.toggle('active',t.dataset.tab===tab)});
  document.querySelectorAll('.tab-content').forEach(function(c){c.classList.toggle('active',c.id==='tab-'+tab)});
}
document.getElementById('tabs').addEventListener('click',function(e){
  var t=e.target.closest?e.target.closest('[data-tab]'):null;
  if(t)switchTab(t.getAttribute('data-tab'));
});
/* PRO_MONEY_TAB_JS_BLOCK1 */
function renderNow(s,sessions){
  document.getElementById('n-sessions').textContent=s.sessions||0;
  document.getElementById('n-turns').textContent=s.turns||0;
  document.getElementById('n-injected').textContent=s.injected_turns||0;
  document.getElementById('n-cached').textContent=s.cache_hit_turns||0;
  var byP={};
  sessions.forEach(function(s){var p=s.provider_id||'unknown';(byP[p]=byP[p]||[]).push(s)});
  var html=Object.entries(byP).sort(function(a,b){return a[0].localeCompare(b[0])}).map(function(entry){
    var prov=entry[0],list=entry[1];
    var cards=list.map(function(s){
      var inj=s.injected_turns||0,cch=s.cache_hit_turns||0;
      return'<div class="sc">'
        +'<div class="st"><span class="sid">'+abbr(s.external_session_id)+'</span>'
        +(s.title?'<span class="stitle">'+s.title+'</span>':'')
        +'<span class="sage">'+rel(s.last_seen)+'</span></div>'
        +'<div class="metrics">'
        +'<span class="pill p-base">'+(s.turn_count||0)+' turns</span>'
        +'<span class="pill p-base">'+fmt(s.total_input_tokens||0)+' in</span>'
        +(inj?'<span class="pill p-inj">'+inj+' injected</span>':'')
        +(cch?'<span class="pill p-cache">'+cch+' cached</span>':'')
        +'</div></div>';
    }).join('');
    return'<div class="pg"><div class="ph">'+prov+'<span class="pc">'+list.length+'</span></div>'+cards+'</div>';
  }).join('');
  document.getElementById('now-sessions').innerHTML=html||'<div style="color:#3e4a5e;font-size:12px">No sessions recorded yet.</div>';
}

function renderQuality(s){
  document.getElementById('q-inj-rate').textContent=(s.injection_rate_pct||0)+'%';
  document.getElementById('q-cache-rate').textContent=(s.cache_hit_rate_pct||0)+'%';
  document.getElementById('q-savings-pct').textContent=(s.savings_pct||0)+'%';
  document.getElementById('q-tok-in').textContent=fmt(s.total_input_tokens||0);
  document.getElementById('q-tok-out').textContent=fmt(s.total_output_tokens||0);
  document.getElementById('q-tok-saved').textContent=fmt(s.total_tokens_saved_est||0);
}

/* PRO_MONEY_TAB_JS_LOADPLAN */

async function load(){
  try{
    var results=await Promise.all([fetch('/api/dashboard/state'),fetch('/api/dashboard/sessions')]);
    if(!results[0].ok||!results[1].ok)throw new Error();
    _state=await results[0].json();
    _sessions=(await results[1].json()).sessions||[];
    if(typeof renderMoney==='function')renderMoney(_state,_sessions);
    renderNow(_state,_sessions);
    renderQuality(_state);
    document.getElementById('err').style.display='none';
    document.getElementById('ts').textContent='Updated '+new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById('err').textContent='MemStrata core offline. Is it running on localhost:8000?';
    document.getElementById('err').style.display='';
    document.getElementById('ts').textContent='Offline';
  }
}

switchTab(_activeTab);
/* PRO_MONEY_TAB_JS_RENDER */
load();
setInterval(load,30000);
</script>
</body>
</html>"""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request) -> str:
    """Local dashboard UI — groups telemetry turns by chat_session_id.

    V5.2-E E.1 — the dashboard template carries placeholder markers
    (``/* PRO_MONEY_TAB_CSS */``, ``<!-- PRO_MONEY_TAB_BODY -->``, etc.)
    where Pro money-tab content goes. When the Pro overlay is mounted
    (``memstrata_pro.api_overlay.mount``), ``app.state.dashboard_extras``
    holds the substitution map and we replace each marker with its Pro
    value. When the overlay isn't mounted (post-split Open running alone),
    the markers stay as inert HTML/JS comments and the page renders with
    only the Now + Quality tabs.

    V5.2-F — live USDCAD FX substitution into the Pro Money-tab init
    block is the Pro overlay's responsibility (it provides a callable
    in the extras map). Open has no FX awareness.
    """
    extras = getattr(request.app.state, "dashboard_extras", None) or {}
    html = _DASHBOARD_HTML
    for marker, value in extras.items():
        if callable(value):
            try:
                value = value()
            except Exception as exc:                          # noqa: BLE001
                _logger.warning("dashboard_extras[%r] callable raised: %s", marker, exc)
                continue
        html = html.replace(marker, value)
    return html


# ---------------------------------------------------------------------------
# V5.2-A Phase 35.3 — indexing progress endpoints + dashboard tab
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> dict:
    """sqlite3.Row -> plain dict for snapshot building."""
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}


@app.get("/api/indexing/state")
def indexing_state(conn: Conn) -> dict:
    """Snapshot of every in-flight / recently-completed indexing job.

    The dashboard polls this every 2 s for live progress; the wizard
    also reads it to surface "Indexing X% complete" in the terminal.
    """
    from memstrata.layer3.ingestion.progress import (
        CONTROL_REGISTRY,
        build_snapshot,
    )
    rows = conn.execute(
        """
        SELECT id, project_id, project_path, phase, files_total, files_processed,
               entities_total, entities_embedded, last_processed_file,
               started_at, completed_at, error
        FROM indexing_jobs
        ORDER BY started_at DESC
        """
    ).fetchall()
    jobs: list[dict] = []
    for r in rows:
        row_dict = {k: r[k] for k in r.keys()}
        snap = build_snapshot(row_dict, control=CONTROL_REGISTRY.get(row_dict["project_id"]))
        jobs.append(snap.to_dict())
    return {"jobs": jobs}


class IndexingControlBody(BaseModel):
    project_id: str


@app.post("/api/indexing/pause")
def indexing_pause(body: IndexingControlBody, conn: Conn) -> dict:
    """User-initiated pause — flips the in-memory pause flag AND
    persists ``indexing_jobs.phase = 'paused'`` so a process restart
    knows we were paused, not crashed."""
    from memstrata.layer3.ingestion.progress import CONTROL_REGISTRY
    state = CONTROL_REGISTRY.get_or_create(body.project_id)
    state.pause_flag.set()
    conn.execute(
        "UPDATE indexing_jobs SET phase='paused' WHERE project_id=? AND phase NOT IN ('complete','failed')",
        (body.project_id,),
    )
    conn.commit()
    return {"ok": True, "project_id": body.project_id, "paused": True}


@app.post("/api/indexing/resume")
def indexing_resume(body: IndexingControlBody, conn: Conn) -> dict:
    """Clear the pause flag. The orchestrator's ``resume()`` figures out
    which phase to re-enter based on persisted counters."""
    from memstrata.layer3.ingestion.progress import CONTROL_REGISTRY
    state = CONTROL_REGISTRY.get_or_create(body.project_id)
    state.pause_flag.clear()
    # We don't auto-restart the orchestrator from here — the dashboard
    # button only flips the flag. The wizard / background thread that
    # owns the orchestrator instance picks up where it left off.
    return {"ok": True, "project_id": body.project_id, "paused": False}


@app.post("/api/indexing/cancel")
def indexing_cancel(body: IndexingControlBody, conn: Conn) -> dict:
    """Hard stop — sets cancel + pause flags, persists 'paused' so the
    user can retry later. We deliberately don't delete the partial
    indexing_jobs row; the resume API recovers it."""
    from memstrata.layer3.ingestion.progress import CONTROL_REGISTRY
    state = CONTROL_REGISTRY.get_or_create(body.project_id)
    state.cancel_flag.set()
    state.pause_flag.set()
    conn.execute(
        "UPDATE indexing_jobs SET phase='paused' WHERE project_id=? AND phase NOT IN ('complete','failed')",
        (body.project_id,),
    )
    conn.commit()
    return {"ok": True, "project_id": body.project_id, "cancelled": True}


_INDEXING_HTML = """<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<title>MemStrata - Indexing</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         background: #0f1419; color: #e6edf3; margin: 0; padding: 24px; }
  h1 { font-size: 22px; margin-top: 0; }
  .job { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
         padding: 16px; margin-bottom: 16px; }
  .job-head { display: flex; justify-content: space-between; align-items: center;
              margin-bottom: 12px; }
  .phase { font-family: ui-monospace, monospace; padding: 4px 8px;
           border-radius: 4px; background: #1f2937; font-size: 12px; }
  .phase.complete { background: #16a34a; color: #fff; }
  .phase.failed   { background: #dc2626; color: #fff; }
  .phase.paused   { background: #ca8a04; color: #fff; }
  .bar { height: 8px; background: #1f2937; border-radius: 4px; overflow: hidden;
         margin: 4px 0 12px; }
  .bar > div { height: 100%; background: #38bdf8; transition: width 0.4s; }
  .meta { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 24px;
          font-size: 13px; color: #9ca3af; }
  .meta b { color: #e6edf3; font-weight: 600; }
  .controls { margin-top: 12px; display: flex; gap: 8px; }
  button { background: #1f2937; color: #e6edf3; border: 1px solid #30363d;
           border-radius: 4px; padding: 6px 12px; cursor: pointer; font-size: 13px; }
  button:hover { background: #30363d; }
  .empty { text-align: center; color: #9ca3af; padding: 40px 0; }
  .reason { color: #fbbf24; font-family: ui-monospace, monospace; }
  .file-trace { font-family: ui-monospace, monospace; font-size: 11px;
                color: #9ca3af; word-break: break-all; margin-top: 4px; }
</style>
</head>
<body>
<h1>Indexing</h1>
<div id=\"root\"><div class=\"empty\">Loading...</div></div>
<script>
function fmtSeconds(s) {
  if (s === null || s === undefined) return '--';
  if (s < 60) return s.toFixed(0) + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + Math.floor(s%60) + 's';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}
function pct(x) { return (x * 100).toFixed(1) + '%'; }
async function call(path, body) {
  return fetch(path, {
    method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify(body),
  }).then(r => r.json());
}
async function refresh() {
  const r = await fetch('/api/indexing/state');
  const data = await r.json();
  const root = document.getElementById('root');
  if (!data.jobs || data.jobs.length === 0) {
    root.innerHTML = '<div class=\"empty\">No indexing jobs in progress.<br>Use <code>memstrata-pro init</code> to index a project.</div>';
    return;
  }
  root.innerHTML = data.jobs.map(j => `
    <div class=\"job\">
      <div class=\"job-head\">
        <div><b>${j.project_path || j.project_id}</b></div>
        <div class=\"phase ${j.phase}\">${j.phase}</div>
      </div>
      <div>files ${j.files_processed} / ${j.files_total} (${pct(j.files_pct)})</div>
      <div class=\"bar\"><div style=\"width: ${j.files_pct*100}%\"></div></div>
      <div>entities ${j.entities_embedded} / ${j.entities_total} (${pct(j.entities_pct)})</div>
      <div class=\"bar\"><div style=\"width: ${j.entities_pct*100}%\"></div></div>
      <div class=\"meta\">
        <div>elapsed:    <b>${fmtSeconds(j.elapsed_seconds)}</b></div>
        <div>ETA:        <b>${fmtSeconds(j.eta_seconds)}</b></div>
        <div>files/sec:  <b>${j.rate_files_per_second ? j.rate_files_per_second.toFixed(2) : '--'}</b></div>
        <div>chunks/sec: <b>${j.rate_entities_per_second ? j.rate_entities_per_second.toFixed(2) : '--'}</b></div>
      </div>
      ${j.soft_pause_reason ? `<div class=\"reason\">paused: ${j.soft_pause_reason}</div>` : ''}
      ${j.last_processed_file ? `<div class=\"file-trace\">last file: ${j.last_processed_file}</div>` : ''}
      ${j.error ? `<div class=\"reason\">error: ${j.error}</div>` : ''}
      <div class=\"controls\">
        ${j.phase === 'paused' || j.is_paused
          ? `<button onclick=\"call('/api/indexing/resume',{project_id:'${j.project_id}'}).then(refresh)\">Resume</button>`
          : `<button onclick=\"call('/api/indexing/pause',{project_id:'${j.project_id}'}).then(refresh)\">Pause</button>`}
        <button onclick=\"if(confirm('Cancel this indexing job?')) call('/api/indexing/cancel',{project_id:'${j.project_id}'}).then(refresh)\">Cancel</button>
      </div>
    </div>
  `).join('');
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


@app.get("/dashboard/indexing", response_class=HTMLResponse)
def dashboard_indexing() -> str:
    """V5.2-A Phase 35.3 — live indexing dashboard tab."""
    return _INDEXING_HTML


# ---------------------------------------------------------------------------
# Phase 32 — NL command interceptor backend endpoints
# ---------------------------------------------------------------------------

class DeleteChatSessionBody(BaseModel):
    chat_session_id: str
    provider_id: str | None = None


@app.post("/chat-session/delete")
def delete_chat_session(body: DeleteChatSessionBody, conn: Conn) -> dict:
    """Delete all MemStrata data for a single chat session.

    Called by the browser extension's NLCommandDetector after the user
    confirms "delete this chat history" / "wipe memory for this chat".
    Hard Rule 66: the caller (extension) must have already shown
    confirmation before reaching this endpoint.
    """
    session_id = body.chat_session_id.strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="chat_session_id is required")

    # Delete telemetry rows first (they hold the FK reference).
    tst_deleted = conn.execute(
        "DELETE FROM telemetry_session_timeline WHERE chat_session_id = ?",
        (session_id,),
    ).rowcount
    rl_deleted = conn.execute(
        "DELETE FROM rationale_log WHERE chat_session_id = ?",
        (session_id,),
    ).rowcount
    cs_deleted = conn.execute(
        "DELETE FROM chat_sessions WHERE id = ?",
        (session_id,),
    ).rowcount
    conn.commit()

    return {
        "deleted": True,
        "chat_session_id": session_id,
        "telemetry_rows_deleted": tst_deleted,
        "rationale_rows_deleted": rl_deleted,
        "session_row_deleted": cs_deleted,
    }


@app.post("/memory/delete-all")
def delete_all_memory(conn: Conn) -> dict:
    """Delete ALL MemStrata data across all chats and providers.

    Called after double-confirmation of "delete all my memory".
    Hard Rule 66: the caller (extension) must have already shown two-step
    confirmation before reaching this endpoint.
    """
    tst_deleted = conn.execute(
        "DELETE FROM telemetry_session_timeline"
    ).rowcount
    rl_deleted = conn.execute(
        "DELETE FROM rationale_log"
    ).rowcount
    cs_deleted = conn.execute(
        "DELETE FROM chat_sessions"
    ).rowcount
    conn.commit()

    return {
        "deleted": True,
        "telemetry_rows_deleted": tst_deleted,
        "rationale_rows_deleted": rl_deleted,
        "session_rows_deleted": cs_deleted,
    }


# V5.2-E E.1: Phase 33 License / plan-feature endpoints
# (/license/current-plan, /license/plan-features, /license/set-plan)
# moved to ``memstrata_pro.api_overlay._register_license_routes``.


# ---------------------------------------------------------------------------
# Phase 34.3 — Relevance-based rewrite context  POST /context/for-chat-rewrite
# Spec: V5_4_PHASE_34_REFINEMENT.md §4
# ---------------------------------------------------------------------------

class ChatRewriteBody(BaseModel):
    chat_session_id: str | None = None
    external_session_id: str | None = None
    draft_prompt: str
    target_token_budget: int = 1500
    provider_id: str


@app.post("/context/for-chat-rewrite")
def context_for_chat_rewrite(body: ChatRewriteBody, conn: Conn) -> dict:
    """Return relevance-scored chat turns for rewrite-mode context injection.

    Accepts either chat_session_id (internal cs_xxx) or external_session_id
    (URL-derived, sent by the browser extension) + provider_id.

    Edge cases handled per spec §5:
      §5.4 draft_too_short — draft < 10 chars, short-circuit, no embedding.
      §5.1 no_history      — session has zero turns.
      §5.2 embeddings_pending — turns exist but none embedded yet; falls back
                                to chronological order, degraded=true.
    """
    now = datetime.now(timezone.utc)

    # Resolve internal chat_session_id — prefer explicit value, else look up by external_session_id
    chat_session_id = body.chat_session_id
    if chat_session_id is None:
        if body.external_session_id is None:
            raise HTTPException(
                status_code=422,
                detail="Either chat_session_id or external_session_id must be provided",
            )
        row = conn.execute(
            "SELECT id FROM chat_sessions WHERE external_session_id = ? AND provider_id = ?",
            (body.external_session_id, body.provider_id),
        ).fetchone()
        if row is None:
            return {
                "retrieved_turns": [],
                "total_session_turns": 0,
                "degraded": False,
                "reason": "no_history",
            }
        chat_session_id = row["id"]

    # §5.4 — short-circuit before any DB work
    if len(body.draft_prompt.strip()) < 10:
        return {"retrieved_turns": [], "degraded": False, "reason": "draft_too_short"}

    # Count total turns and embedding status for this session
    stats = conn.execute(
        """
        SELECT
            COUNT(tst.id)                                                          AS total_turns,
            COUNT(CASE WHEN eq.completed_at IS NOT NULL          THEN 1 END)       AS with_embeddings,
            COUNT(CASE WHEN eq.id IS NULL OR eq.completed_at IS NULL THEN 1 END)   AS pending
        FROM telemetry_session_timeline tst
        LEFT JOIN embedding_queue eq ON eq.timeline_id = tst.id
        WHERE tst.chat_session_id = ?
        """,
        (chat_session_id,),
    ).fetchone()

    total_turns: int = stats["total_turns"] or 0
    with_embeddings: int = stats["with_embeddings"] or 0
    pending: int = stats["pending"] or 0

    # §5.1 — empty session
    if total_turns == 0:
        return {
            "retrieved_turns": [],
            "total_session_turns": 0,
            "degraded": False,
            "reason": "no_history",
        }

    # Embed the draft prompt synchronously (only allowed sync embed per §3.2)
    query_embedding = _retrieval.embed_text(body.draft_prompt)

    # §5.2 — embeddings not yet available: chronological fallback
    if query_embedding is None or with_embeddings == 0:
        fallback = conn.execute(
            """
            SELECT id, role, text, recorded_at
              FROM telemetry_session_timeline
             WHERE chat_session_id = ?
               AND text IS NOT NULL
               AND length(text) >= 50
             ORDER BY recorded_at DESC
             LIMIT ?
            """,
            (chat_session_id, _retrieval.CANDIDATE_K),
        ).fetchall()

        turns = [
            {
                "timeline_id":    r["id"],
                "role":           r["role"],
                "text":           r["text"],
                "captured_at":    _retrieval.recorded_at_to_iso(r["recorded_at"]),
                "similarity_score": None,
                "recency_score":    None,
                "final_score":      None,
                "age_human":      _retrieval.age_human(parse_recorded_at(r["recorded_at"]), now),
            }
            for r in reversed(fallback)  # oldest-first
        ]

        return {
            "retrieved_turns":       turns,
            "token_budget_used":     sum(_retrieval.estimate_tokens(t["text"]) for t in turns),
            "token_budget_total":    body.target_token_budget,
            "total_session_turns":   total_turns,
            "turns_with_embeddings": with_embeddings,
            "turns_pending_embedding": pending,
            "turns_considered":      len(fallback),
            "turns_returned":        len(turns),
            "degraded":              True,
            "reason":                "embeddings_pending",
        }

    # Normal path — vector search (Issue 1 decision: JOIN through chat_sessions for provider_id)
    candidates = conn.execute(
        """
        SELECT
            tst.id,
            tst.role,
            tst.text,
            tst.recorded_at,
            vec_distance_cosine(ttv.embedding, ?) AS similarity_distance
          FROM telemetry_timeline_vec ttv
          JOIN telemetry_session_timeline tst ON ttv.timeline_id = tst.id
          JOIN chat_sessions cs ON tst.chat_session_id = cs.id
         WHERE tst.chat_session_id = ?
           AND cs.provider_id = ?
           AND length(tst.text) >= 50
         ORDER BY similarity_distance ASC
         LIMIT ?
        """,
        (json.dumps(query_embedding), chat_session_id, body.provider_id, _retrieval.CANDIDATE_K),
    ).fetchall()

    # Score, budget-trim, then re-sort chronologically
    scored: list[dict] = []
    for r in candidates:
        similarity = 1.0 - float(r["similarity_distance"])
        final, sim, rec = _retrieval.compute_final_score(similarity, r["recorded_at"], now)
        scored.append({
            "timeline_id":    r["id"],
            "role":           r["role"],
            "text":           r["text"],
            "captured_at":    _retrieval.recorded_at_to_iso(r["recorded_at"]),
            "similarity_score": round(sim, 4),
            "recency_score":    round(rec, 4),
            "final_score":      round(final, 4),
            "age_human":      _retrieval.age_human(parse_recorded_at(r["recorded_at"]), now),
        })

    selected = _retrieval.fit_to_budget(scored, body.target_token_budget)
    selected.sort(key=lambda t: t["captured_at"])  # chronological order for injection

    return {
        "retrieved_turns":         selected,
        "token_budget_used":       sum(_retrieval.estimate_tokens(t["text"]) for t in selected),
        "token_budget_total":      body.target_token_budget,
        "total_session_turns":     total_turns,
        "turns_with_embeddings":   with_embeddings,
        "turns_pending_embedding": pending,
        "turns_considered":        len(candidates),
        "turns_returned":          len(selected),
        "degraded":                False,
    }


# ---------------------------------------------------------------------------
# Phase 34.6 — Per-rewrite telemetry  POST /telemetry/rewrite
# Spec: V5_4_PHASE_34_REFINEMENT.md §7
# ---------------------------------------------------------------------------

class RewriteTelemetryBody(BaseModel):
    rewrite_id: str
    external_session_id: str | None = None
    provider_id: str
    draft_prompt_chars: int
    retrieved_turn_count: int
    retrieved_turn_avg_similarity: float | None = None
    retrieved_turn_age_dist_hours: list[float] | None = None
    user_confirmed: bool
    delimiter_format: str = "xml_tags"
    token_budget_used: int | None = None
    token_budget_total: int | None = None
    degraded: bool
    degraded_reason: str | None = None


@app.post("/telemetry/rewrite")
def record_rewrite_telemetry(body: RewriteTelemetryBody, conn: Conn) -> dict:
    """Store one per-rewrite telemetry event. Idempotent on rewrite_id."""
    # Resolve internal chat_session_id from external_session_id if available
    chat_session_id: str | None = None
    if body.external_session_id:
        row = conn.execute(
            "SELECT id FROM chat_sessions WHERE external_session_id = ? AND provider_id = ?",
            (body.external_session_id, body.provider_id),
        ).fetchone()
        if row:
            chat_session_id = row["id"]

    age_dist_json = json.dumps(body.retrieved_turn_age_dist_hours) if body.retrieved_turn_age_dist_hours else None

    conn.execute(
        """
        INSERT OR IGNORE INTO rewrite_telemetry (
            rewrite_id, chat_session_id, external_session_id, provider_id,
            draft_prompt_chars, retrieved_turn_count, retrieved_turn_avg_similarity,
            retrieved_turn_age_dist_hours, user_confirmed, delimiter_format,
            token_budget_used, token_budget_total, degraded, degraded_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            body.rewrite_id,
            chat_session_id,
            body.external_session_id,
            body.provider_id,
            body.draft_prompt_chars,
            body.retrieved_turn_count,
            body.retrieved_turn_avg_similarity,
            age_dist_json,
            1 if body.user_confirmed else 0,
            body.delimiter_format,
            body.token_budget_used,
            body.token_budget_total,
            1 if body.degraded else 0,
            body.degraded_reason,
        ),
    )
    conn.commit()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# WebSocket — GET /telemetry/live  (live dashboard feed)
#
# Auth: accepts the API key from:
#   1. ?api_key=<key> query parameter  (browser WebSocket API cannot set headers)
#   2. X-API-Key: <key> request header  (non-browser clients)
#   3. Authorization: Bearer <key> header
#
# When ML_API_KEY is not configured, all connections are accepted (dev mode).
# ---------------------------------------------------------------------------

import asyncio as _asyncio  # already available; local alias avoids name collision


@app.websocket("/telemetry/live")
async def telemetry_live(
    websocket: WebSocket,
    api_key: str = Query(default=""),
) -> None:
    expected = os.environ.get("ML_API_KEY", "")
    if expected:
        resolved = (
            api_key
            or websocket.headers.get("x-api-key", "")
            or websocket.headers.get("authorization", "").removeprefix("Bearer ").strip()
        )
        if resolved != expected:
            await websocket.close(code=4003)
            return

    await websocket.accept()

    last_fp = ""
    try:
        while True:
            await _asyncio.sleep(1.0)
            try:
                conn = sqlite3.connect(
                    str(get_db_path()), check_same_thread=False, timeout=5.0
                )
                conn.row_factory = sqlite3.Row
                try:
                    snap = _compute_dashboard_state(conn)
                finally:
                    conn.close()
            except Exception as exc:
                _logger.debug("telemetry_live snapshot error: %s", exc)
                continue

            fp = hashlib.md5(
                json.dumps(snap, sort_keys=True, default=str).encode()
            ).hexdigest()
            if fp != last_fp:
                await websocket.send_json({"type": "telemetry_update", **snap})
                last_fp = fp
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        _logger.warning("telemetry_live closed unexpectedly: %s", exc)
