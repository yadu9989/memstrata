"""Memory Layer MIT core — FastAPI server.

Started by `memory-layer api` → uvicorn.run("memory_layer.layer3.api_server:app").
Consumed by:
  - The browser extension (POST /telemetry/session, GET /health, GET /baseline/status)
  - The harness (GET /context/injection, POST /sessions, POST /sessions/{id}/close,
                  POST /telemetry/session)
  - New in Phase 31: GET /context/for-chat (session-scoped retrieval)
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

_logger = logging.getLogger(__name__)

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from memory_layer.layer3._db import (
    get_conn,
    get_db_path,
    init_db,
    is_valid_external_session_id,
    new_id,
    upsert_chat_session,
)
from memory_layer.layer3 import feature_gate as fg
from memory_layer.layer3.pricing.lookup import get_rates, compute_input_savings_usd, compute_cache_savings_usd
from memory_layer.layer3.pricing.openrouter_sync import sync_loop as _pricing_sync_loop
from memory_layer.layer3.baseline.cohort import (
    ensure_table as _ensure_baseline_table,
    is_in_baseline_window,
    days_remaining as _baseline_days_remaining,
    compute_and_close_baseline,
)


# ---------------------------------------------------------------------------
# App + lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = sqlite3.connect(str(get_db_path()), check_same_thread=False)
    try:
        init_db(conn)
        _ensure_baseline_table(conn)
    finally:
        conn.close()

    def _conn_factory():
        c = sqlite3.connect(str(get_db_path()), check_same_thread=False, timeout=10.0)
        c.row_factory = sqlite3.Row
        return c

    import asyncio
    task = asyncio.create_task(_pricing_sync_loop(_conn_factory))
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Memory Layer Core", version="0.5.4", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "alive", "version": "0.5.4"}


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
def record_turn(body: TurnTelemetryBody, conn: Conn) -> dict:
    chat_session_id: str | None = None

    # Phase 33 — validated upsert: strip, validate format, then link to chat session.
    # On any failure we degrade gracefully: turn is stored with chat_session_id=NULL.
    # Telemetry must never return an error to the caller.
    if body.external_session_id and body.provider:
        ext_id = body.external_session_id.strip()
        provider = body.provider.strip()
        if ext_id and provider and is_valid_external_session_id(ext_id):
            try:
                chat_session_id = upsert_chat_session(conn, provider, ext_id)
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
    measurement_basis: str = "input_measured"
    in_baseline = False

    if body.project_id:
        try:
            in_baseline = is_in_baseline_window(body.project_id, conn)
            if not in_baseline:
                compute_and_close_baseline(body.project_id, conn)
        except Exception as exc:
            _logger.debug("baseline check failed for %s: %s", body.project_id, exc)

    if (
        not in_baseline
        and body.provider
        and body.model
        and body.actual_input_tokens is not None
    ):
        rates = get_rates(body.provider, body.model, conn=conn)
        if rates is not None:
            if body.baseline_no_context is not None and body.injected:
                saved_input_usd = compute_input_savings_usd(
                    body.baseline_no_context,
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

    conn.execute(
        """
        INSERT INTO telemetry_session_timeline (
            session_id, turn_id, project_id,
            provider, model,
            actual_input_tokens, actual_output_tokens,
            chat_session_id,
            role, text, char_count,
            baseline_no_context, injected, cache_hit_estimated,
            saved_input_cost_usd, saved_cache_cost_usd,
            measurement_basis, baseline_period
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            body.session_id, body.turn_id, body.project_id,
            body.provider, body.model,
            body.actual_input_tokens, body.actual_output_tokens,
            chat_session_id,
            body.role, body.text, body.char_count,
            body.baseline_no_context,
            1 if body.injected else 0,
            1 if body.cache_hit_estimated else 0,
            round(saved_input_usd, 6),
            round(saved_cache_usd, 6),
            measurement_basis,
            1 if in_baseline else 0,
        ),
    )
    conn.commit()
    return {"id": body.session_id, "received_at": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# Context injection — GET /context/injection  (called by the harness)
# Stub: the full pipeline (sqlite-vec, nomic-embed-text) lives in V3 MIT core.
# Returns an empty block so the harness skips injection without crashing.
# ---------------------------------------------------------------------------

@app.get("/context/injection")
def context_injection(project_id: str = Query(default="default")) -> dict:
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
def baseline_status(conn: Conn, project_id: str = Query(default="default")) -> dict:
    try:
        in_baseline = is_in_baseline_window(project_id, conn)
        remaining = _baseline_days_remaining(project_id, conn) if in_baseline else None
    except Exception as exc:
        _logger.warning("baseline_status error for %s: %s", project_id, exc)
        in_baseline = False
        remaining = None
    return {"in_baseline": in_baseline, "days_remaining": remaining}


# ---------------------------------------------------------------------------
# Phase 32 — Dashboard: state, session list, and HTML UI
# ---------------------------------------------------------------------------

@app.get("/api/dashboard/state")
def dashboard_state(conn: Conn) -> dict:
    """Aggregate stats consumed by the VS Code status bar (polls every 5 s)."""
    # INNER JOIN ensures orphaned FK turns (chat_session_id with no matching
    # chat_sessions row) are excluded from both the session count and the metrics.
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

    total_turns = row["turn_count"] or 0
    injected    = row["injected_turns"] or 0
    cached      = row["cache_turns"] or 0

    injection_rate = injected / total_turns if total_turns else 0.0
    cache_rate     = cached   / injected    if injected    else 0.0
    # savings_pct: fraction of all chat-linked turns that hit the cache
    savings_pct = round(cached / total_turns * 100, 1) if total_turns else 0.0
    recall_pct  = round(injection_rate * 100, 1)

    # Compound token savings: sum per-session savings across all chat sessions.
    # Per-session: avg_context_overhead × cache_hit_turns (same formula as
    # /api/dashboard/sessions tokens_saved_est column).
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
              FROM telemetry_session_timeline tst
              JOIN chat_sessions cs ON cs.id = tst.chat_session_id
             GROUP BY tst.chat_session_id
        )
        SELECT COALESCE(SUM(session_saved), 0) AS total_saved
          FROM per_session
        """
    ).fetchone()
    total_tokens_saved_est = int(saved_row["total_saved"] or 0)

    return {
        "status":                 "alive",
        "sessions":               row["session_count"] or 0,
        "turns":                  total_turns,
        "total_input_tokens":     row["total_input"]  or 0,
        "total_output_tokens":    row["total_output"] or 0,
        "injected_turns":         injected,
        "cache_hit_turns":        cached,
        "injection_rate_pct":     recall_pct,
        "cache_hit_rate_pct":     round(cache_rate * 100, 1),
        "savings_pct":            savings_pct,
        "recall_pct":             recall_pct,
        "total_tokens_saved_est": total_tokens_saved_est,
    }


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
            )                                             AS tokens_saved_est
          FROM chat_sessions cs
          LEFT JOIN telemetry_session_timeline tst ON tst.chat_session_id = cs.id
         GROUP BY cs.id
         ORDER BY cs.last_seen DESC
         LIMIT 200
        """
    ).fetchall()

    # Harness-only sessions (chat_session_id IS NULL, grouped by session_id)
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
            MIN(recorded_at)                              AS first_seen,
            MAX(recorded_at)                              AS last_seen
          FROM telemetry_session_timeline
         WHERE chat_session_id IS NULL
         GROUP BY session_id
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
            "tokens_saved_est":    0,  # harness sessions have no baseline data
        }
        for r in h_rows
    ]

    all_sessions = chat_sessions + harness_sessions
    return {"sessions": all_sessions, "total_count": len(all_sessions)}


_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Memory Layer Pro</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;font-size:13px;background:#0f1117;color:#c8d0da;min-height:100vh}
header{padding:12px 20px;border-bottom:1px solid #222733;display:flex;align-items:center;gap:12px}
h1{font-size:14px;font-weight:600;color:#e8eaf0;letter-spacing:.01em}
#ts{font-size:11px;color:#4e5570;margin-left:auto}
#summary{display:flex;gap:8px;flex-wrap:wrap;padding:12px 20px;border-bottom:1px solid #1e222e}
.stat{background:#161b24;border:1px solid #222733;border-radius:6px;padding:6px 12px}
.sv{font-size:15px;font-weight:600;color:#7dd3a8}
.sl{font-size:11px;color:#5a6478;margin-left:5px}
#sessions{padding:16px 20px}
.pg{margin-bottom:18px}
.ph{font-size:10px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:#5a6478;margin-bottom:7px;display:flex;align-items:center;gap:7px}
.pc{background:#1a1e2c;border-radius:10px;padding:1px 7px;font-weight:400;font-size:10px}
.sc{background:#13171f;border:1px solid #1e2330;border-radius:6px;padding:9px 13px;margin-bottom:5px}
.st{display:flex;align-items:center;gap:6px;margin-bottom:6px}
.sid{font-family:ui-monospace,monospace;font-size:11px;color:#8aa4c0}
.stitle{font-size:12px;color:#b8c4d4}
.sage{font-size:11px;color:#3e4a5e;margin-left:auto}
.metrics{display:flex;gap:6px;flex-wrap:wrap}
.pill{font-size:11px;border-radius:10px;padding:2px 8px}
.p-base{background:#1a1e2c;color:#7080a0}
.p-inj{background:#172519;color:#6dba8c}
.p-cache{background:#152030;color:#5a9ec8}
.p-save{background:#1e1830;color:#a07ecf}
#empty{color:#3e4a5e;padding:8px 0;font-size:12px}
#err{padding:12px 20px;color:#c06a6a;font-size:12px;display:none}
#tabs{display:flex;gap:0;padding:0 20px;border-bottom:1px solid #1e222e}
.tab{padding:8px 14px;font-size:12px;color:#5a6478;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}
.tab.active{color:#7dd3a8;border-bottom-color:#7dd3a8}
.tab.hidden{display:none}
</style>
</head>
<body>
<header><h1>Memory Layer Pro</h1><span id="ts">Loading…</span></header>
<div id="summary"></div>
<div id="tabs">
  <span class="tab active" data-tab="all">All Sessions</span>
  <span class="tab" data-tab="chat">Chat</span>
  <span class="tab" id="tab-coding" data-tab="coding">Coding</span>
</div>
<div id="sessions"><div id="empty" style="display:none">No sessions recorded yet.</div></div>
<div id="err"></div>
<script>
var _activeTab='all';
var _allSessions=[];
function fmt(n){if(n==null)return'–';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'K';return''+n}
function rel(ts){if(!ts)return'';const d=(Date.now()-new Date(ts).getTime())/1000;if(d<60)return'just now';if(d<3600)return~~(d/60)+'m ago';if(d<86400)return~~(d/3600)+'h ago';return~~(d/86400)+'d ago'}
function abbr(s){if(!s)return'–';return s.length>12?s.slice(0,12)+'…':s}
async function applyPlanGating(){try{const r=await fetch('/license/plan-features');if(!r.ok)return;const d=await r.json();const f=d.features||[];const chatOnly=f.includes('money_tab_chat_only')&&!f.includes('money_tab');if(chatOnly){const ct=document.getElementById('tab-coding');if(ct){ct.classList.add('hidden');if(_activeTab==='coding')switchTab('chat');}}}catch{}}
function switchTab(tab){_activeTab=tab;document.querySelectorAll('.tab').forEach(t=>{t.classList.toggle('active',t.dataset&&t.dataset.tab===tab);});renderSessions(_allSessions);}
document.getElementById('tabs').addEventListener('click',function(e){const t=e.target.closest?e.target.closest('[data-tab]'):null;if(t&&!t.classList.contains('hidden'))switchTab(t.getAttribute('data-tab'));});
async function load(){
  try{
    const[sr,dr]=await Promise.all([fetch('/api/dashboard/state'),fetch('/api/dashboard/sessions')]);
    if(!sr.ok||!dr.ok)throw new Error();
    renderSummary(await sr.json());
    _allSessions=(await dr.json()).sessions||[];
    renderSessions(_allSessions);
    document.getElementById('err').style.display='none';
    document.getElementById('ts').textContent='Updated '+new Date().toLocaleTimeString();
  }catch{
    document.getElementById('err').textContent='Memory Layer core offline. Is it running on localhost:8000?';
    document.getElementById('err').style.display='';
    document.getElementById('ts').textContent='Offline';
  }
}
function renderSummary(s){
  const items=[
    [s.sessions??0,'threads'],
    [s.turns??0,'turns'],
    [fmt(s.total_input_tokens??0),'tokens in'],
    [(s.injection_rate_pct??0)+'%','injected'],
    [(s.cache_hit_rate_pct??0)+'%','cache hit'],
    [fmt(s.total_tokens_saved_est??0),'tokens saved'],
  ];
  document.getElementById('summary').innerHTML=items.map(([v,l])=>
    `<div class="stat"><span class="sv">${v}</span><span class="sl">${l}</span></div>`
  ).join('');
}
function renderSessions(sessions){
  var filtered=sessions;
  if(_activeTab==='chat') filtered=sessions.filter(function(s){return s.chat_session_id!=null;});
  else if(_activeTab==='coding') filtered=sessions.filter(function(s){return s.chat_session_id==null;});
  const el=document.getElementById('sessions');
  const em=document.getElementById('empty');
  if(!filtered.length){em.style.display='';el.innerHTML='';el.appendChild(em);return}
  sessions=filtered;
  em.style.display='none';
  const byP={};
  for(const s of sessions){const p=s.provider_id||'unknown';(byP[p]=byP[p]||[]).push(s)}
  el.innerHTML=Object.entries(byP).sort(([a],[b])=>a.localeCompare(b)).map(([prov,list])=>{
    const cards=list.map(s=>{
      const ti=s.total_input_tokens||0,to=s.total_output_tokens||0;
      const inj=s.injected_turns||0,cch=s.cache_hit_turns||0;
      return`<div class="sc">
<div class="st">
  <span class="sid">${abbr(s.external_session_id)}</span>
  ${s.title?`<span class="stitle">${s.title}</span>`:''}
  <span class="sage">${rel(s.last_seen)}</span>
</div>
<div class="metrics">
  <span class="pill p-base">${s.turn_count||0} turns</span>
  <span class="pill p-base">${fmt(ti)} in</span>
  <span class="pill p-base">${fmt(to)} out</span>
  ${inj?`<span class="pill p-inj">${inj} injected</span>`:''}
  ${cch?`<span class="pill p-cache">${cch} cached</span>`:''}
  ${s.tokens_saved_est?`<span class="pill p-save">${fmt(s.tokens_saved_est)} saved</span>`:''}
</div>
</div>`;
    }).join('');
    return`<div class="pg"><div class="ph">${prov}<span class="pc">${list.length}</span></div>${cards}</div>`;
  }).join('');
}
applyPlanGating();
load();
setInterval(load,30000);
</script>
</body>
</html>"""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    """Local dashboard UI — groups telemetry turns by chat_session_id."""
    return _DASHBOARD_HTML


# ---------------------------------------------------------------------------
# Phase 32 — NL command interceptor backend endpoints
# ---------------------------------------------------------------------------

class DeleteChatSessionBody(BaseModel):
    chat_session_id: str
    provider_id: str | None = None


@app.post("/chat-session/delete")
def delete_chat_session(body: DeleteChatSessionBody, conn: Conn) -> dict:
    """Delete all Memory Layer data for a single chat session.

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
    """Delete ALL Memory Layer data across all chats and providers.

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


# ---------------------------------------------------------------------------
# Phase 33 — License / plan-feature endpoints
# ---------------------------------------------------------------------------

class SetPlanBody(BaseModel):
    plan: str


@app.get("/license/current-plan")
def get_current_plan(conn: Conn) -> dict:
    """Return the active plan name stored in settings."""
    plan = fg.get_current_plan(conn)
    return {"plan": plan}


@app.get("/license/plan-features")
def get_plan_features(conn: Conn) -> dict:
    """Return the feature flags enabled for the current plan.

    Consumed by:
    - harness MemoryLayerClient.is_feature_active()
    - browser extension FeatureGate
    - VS Code extension (future)
    """
    plan = fg.get_current_plan(conn)
    features = fg.get_plan_features(conn, plan)
    return {"plan": plan, "features": features}


@app.post("/license/set-plan")
def set_plan(body: SetPlanBody, conn: Conn) -> dict:
    """Set the active plan. Called by the Stripe webhook on subscription change."""
    try:
        fg.set_current_plan(conn, body.plan)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"plan": body.plan, "features": fg.get_plan_features(conn, body.plan)}
