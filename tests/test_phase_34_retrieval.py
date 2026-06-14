"""Tests for Phase 34.3 — POST /context/for-chat-rewrite.

Covers:
  - Pure-function unit tests: scoring blend, budget enforcement, age_human.
  - Endpoint edge cases: draft_too_short, no_history, embeddings_pending fallback.
  - Normal path: chitchat filter, token budget skip (not truncate), temporal
    re-sort (chronological output after relevance-based selection).
  - Spec §8.1 tests: test_relevance_scoring_blend, test_chitchat_filter_drops_short_turns,
    test_token_budget_skips_oversize_turns, test_temporal_supersession,
    test_draft_too_short_returns_short_circuit, test_ingest_path_stays_fast.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Whole file requires sqlite-vec — context-rewrite tests query telemetry_timeline_vec.
pytestmark = pytest.mark.requires_sqlite_vec

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ML_DB_PATH", str(tmp_path / "test_retrieval.db"))


@pytest.fixture
def client(isolated_db):
    from memstrata.layer3.api_server import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_path(isolated_db):
    from memstrata.layer3._db import get_db_path
    return get_db_path()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_vec(dim: int = 768, seed: float = 1.0) -> list[float]:
    """768-dim unit vector along axis 0 (modified by seed for variety)."""
    v = [0.0] * dim
    v[0] = seed
    mag = sum(x ** 2 for x in v) ** 0.5
    return [x / mag for x in v]


def _insert_turn_with_embedding(db_path, *, chat_session_id: str, text: str,
                                 role: str = "user", embedding: list[float],
                                 recorded_at: str | None = None) -> int:
    """Insert a timeline row + embedding directly; return timeline_id."""
    from memstrata.layer3._db import _load_vec_extension
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    _load_vec_extension(conn)

    if recorded_at is None:
        recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    conn.execute(
        """
        INSERT INTO telemetry_session_timeline
            (session_id, turn_id, project_id, chat_session_id, role, text, recorded_at)
        VALUES ('s', (SELECT COALESCE(MAX(turn_id),0)+1 FROM telemetry_session_timeline), 'p', ?, ?, ?, ?)
        """,
        (chat_session_id, role, text, recorded_at),
    )
    tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT OR IGNORE INTO embedding_queue (timeline_id) VALUES (?)", (tid,)
    )
    conn.execute(
        "INSERT OR REPLACE INTO telemetry_timeline_vec (timeline_id, embedding) VALUES (?, ?)",
        (tid, json.dumps(embedding)),
    )
    conn.execute(
        "UPDATE embedding_queue SET completed_at = datetime('now') WHERE timeline_id = ?",
        (tid,),
    )
    conn.commit()
    conn.close()
    return tid


def _ensure_chat_session(db_path, *, provider_id: str, chat_session_id: str) -> None:
    """Insert a chat_sessions row if it doesn't already exist."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute(
        """
        INSERT OR IGNORE INTO chat_sessions (id, provider_id, external_session_id)
        VALUES (?, ?, ?)
        """,
        (chat_session_id, provider_id, "ext-" + chat_session_id),
    )
    conn.commit()
    conn.close()


def _post_rewrite(client, *, chat_session_id: str, draft: str,
                   provider_id: str = "anthropic", budget: int = 1500) -> dict:
    resp = client.post("/context/for-chat-rewrite", json={
        "chat_session_id": chat_session_id,
        "draft_prompt": draft,
        "target_token_budget": budget,
        "provider_id": provider_id,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# §8.1 — Pure-function unit tests (no HTTP, no DB)
# ---------------------------------------------------------------------------

class TestScoringBlend:
    """test_relevance_scoring_blend — §8.1"""

    def _now_minus(self, days: float) -> datetime:
        return datetime.now(timezone.utc) - timedelta(days=days)

    def test_formula_alpha_70(self):
        """final_score = 0.7*sim + 0.3*recency exactly."""
        from memstrata.layer3.retrieval import compute_final_score
        now = datetime.now(timezone.utc)
        # Use a timestamp 1 second in the past to avoid sub-second rounding in strftime.
        recorded_str = (now - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        final, sim, rec = compute_final_score(0.8, recorded_str, now, alpha=0.7)
        assert sim == pytest.approx(0.8, abs=1e-6)
        # recency for ~1 second old ≈ 1.0 (half-life is 3 days); allow 1e-3 slack
        assert rec == pytest.approx(1.0, abs=1e-3)
        assert final == pytest.approx(0.7 * 0.8 + 0.3 * rec, abs=1e-6)

    def test_recency_decays_with_age(self):
        from memstrata.layer3.retrieval import RECENCY_HALF_LIFE_SECONDS, compute_final_score
        now = datetime.now(timezone.utc)
        # At exactly 3 days old, recency = 0.5 ** 1 = 0.5
        three_days_ago = now - timedelta(seconds=RECENCY_HALF_LIFE_SECONDS)
        recorded_str = three_days_ago.strftime("%Y-%m-%d %H:%M:%S")
        _, _, rec = compute_final_score(0.0, recorded_str, now)
        assert rec == pytest.approx(0.5, abs=1e-3)

    def test_high_similarity_beats_high_recency(self):
        """A very similar but older turn should outscore a dissimilar recent one."""
        from memstrata.layer3.retrieval import compute_final_score
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        new = (now - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        final_old, _, _ = compute_final_score(0.95, old, now)  # high sim, old
        final_new, _, _ = compute_final_score(0.10, new, now)  # low sim, new
        assert final_old > final_new

    def test_equal_similarity_prefers_newer(self):
        """When similarity is the same, newer turn wins on final_score."""
        from memstrata.layer3.retrieval import compute_final_score
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        new = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        final_old, _, _ = compute_final_score(0.7, old, now)
        final_new, _, _ = compute_final_score(0.7, new, now)
        assert final_new > final_old


class TestTokenBudget:
    """test_token_budget_skips_oversize_turns — §8.1 (Hard Rule 68)"""

    def _make_turn(self, text: str, final_score: float) -> dict:
        return {"text": text, "final_score": final_score, "timeline_id": 1,
                "role": "user", "captured_at": "2026-01-01T00:00:00Z",
                "similarity_score": 0.5, "recency_score": 0.5, "age_human": "1 day ago"}

    def test_skips_turn_that_exceeds_budget(self):
        """A turn whose token cost alone exceeds the budget must be skipped."""
        from memstrata.layer3.retrieval import estimate_tokens, fit_to_budget
        long_text = "x" * 800  # 800 chars = 200 tokens
        short_text = "y" * 40  # 40 chars = 10 tokens
        turns = [
            self._make_turn(long_text, 0.9),   # high score but too big
            self._make_turn(short_text, 0.5),  # low score but fits
        ]
        selected = fit_to_budget(turns, token_budget=50)
        assert len(selected) == 1
        assert selected[0]["text"] == short_text, "Oversize turn must be skipped, not truncated"

    def test_never_truncates_text(self):
        """Text returned by fit_to_budget must be identical to input text."""
        from memstrata.layer3.retrieval import fit_to_budget
        turns = [self._make_turn("hello world this is a sentence", 0.8)]
        selected = fit_to_budget(turns, token_budget=1000)
        assert selected[0]["text"] == "hello world this is a sentence"

    def test_greedy_fills_to_budget(self):
        """Multiple small turns should be packed in until budget is exhausted."""
        from memstrata.layer3.retrieval import estimate_tokens, fit_to_budget
        turns = [self._make_turn("a" * 40, float(i)) for i in range(10)]
        budget = estimate_tokens("a" * 40) * 3  # room for exactly 3 turns
        selected = fit_to_budget(turns, token_budget=budget)
        assert len(selected) == 3

    def test_selects_highest_scoring_within_budget(self):
        """Greedy fill must pick turns by descending final_score."""
        from memstrata.layer3.retrieval import fit_to_budget
        turns = [
            self._make_turn("low  score turn abc", 0.1),
            self._make_turn("high score turn xyz", 0.9),
        ]
        selected = fit_to_budget(turns, token_budget=10)
        # Only one fits; it should be the high-score one
        assert selected[0]["final_score"] == pytest.approx(0.9)

    def test_estimate_tokens_is_len_div_4(self):
        from memstrata.layer3.retrieval import estimate_tokens
        assert estimate_tokens("a" * 100) == 25
        assert estimate_tokens("") == 0


class TestAgeHuman:
    def test_just_now(self):
        from memstrata.layer3.retrieval import age_human
        now = datetime.now(timezone.utc)
        assert age_human(now - timedelta(seconds=30), now) == "just now"

    def test_minutes(self):
        from memstrata.layer3.retrieval import age_human
        now = datetime.now(timezone.utc)
        assert "minute" in age_human(now - timedelta(minutes=5), now)

    def test_hours(self):
        from memstrata.layer3.retrieval import age_human
        now = datetime.now(timezone.utc)
        assert "hour" in age_human(now - timedelta(hours=3), now)

    def test_days(self):
        from memstrata.layer3.retrieval import age_human
        now = datetime.now(timezone.utc)
        assert "day" in age_human(now - timedelta(days=5), now)

    def test_singular_vs_plural(self):
        from memstrata.layer3.retrieval import age_human
        now = datetime.now(timezone.utc)
        assert age_human(now - timedelta(hours=1), now) == "1 hour ago"
        assert age_human(now - timedelta(hours=2), now) == "2 hours ago"


# ---------------------------------------------------------------------------
# Edge-case endpoint tests (§5.1, §5.2, §5.4)
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_draft_too_short_returns_short_circuit(self, client):
        """§5.4 — < 10 chars returns immediately without any DB work."""
        data = _post_rewrite(client, chat_session_id="cs_any", draft="hi",
                              provider_id="openai")
        assert data["retrieved_turns"] == []
        assert data["degraded"] is False
        assert data["reason"] == "draft_too_short"

    def test_draft_exactly_9_chars_is_too_short(self, client):
        data = _post_rewrite(client, chat_session_id="cs_any", draft="123456789",
                              provider_id="openai")
        assert data["reason"] == "draft_too_short"

    def test_draft_exactly_10_chars_is_not_too_short(self, client, db_path):
        """10-char draft should NOT short-circuit (proceeds to no_history instead)."""
        data = _post_rewrite(client, chat_session_id="cs_none", draft="1234567890",
                              provider_id="openai")
        # Session doesn't exist → no_history, not draft_too_short
        assert data.get("reason") == "no_history"

    def test_empty_session_returns_no_history(self, client):
        """§5.1 — Session with no turns returns no_history, degraded=False."""
        data = _post_rewrite(client, chat_session_id="cs_empty",
                              draft="help me debug the auth flow", provider_id="anthropic")
        assert data["retrieved_turns"] == []
        assert data["degraded"] is False
        assert data["reason"] == "no_history"
        assert data["total_session_turns"] == 0

    def test_embeddings_pending_fallback_is_chronological(self, client, db_path):
        """§5.2 — Turns exist but no embeddings; falls back to recent turns, degraded=True."""
        cs_id = "cs_pending"
        _ensure_chat_session(db_path, provider_id="anthropic", chat_session_id=cs_id)

        # Insert turns WITHOUT embeddings (via raw SQL — bypassing enqueue_for_embedding)
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        for i in range(3):
            recorded = (datetime.now(timezone.utc) - timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO telemetry_session_timeline "
                "(session_id, turn_id, project_id, chat_session_id, role, text, recorded_at) "
                "VALUES ('s', ?, 'p', ?, 'user', ?, ?)",
                (i + 1, cs_id, f"this is turn number {i+1} with enough text to pass the filter", recorded),
            )
        conn.commit()
        conn.close()

        # No embeddings in queue → with_embeddings = 0
        with patch("memstrata.layer3.retrieval.embed_text", return_value=None):
            data = _post_rewrite(client, chat_session_id=cs_id,
                                  draft="debug the authentication callback", provider_id="anthropic")

        assert data["degraded"] is True
        assert data["reason"] == "embeddings_pending"
        assert data["turns_with_embeddings"] == 0
        turns = data["retrieved_turns"]
        assert len(turns) > 0
        # All similarity scores must be null in fallback
        for t in turns:
            assert t["similarity_score"] is None
            assert t["final_score"] is None
        # Must be chronological (oldest captured_at first)
        captured_ats = [t["captured_at"] for t in turns]
        assert captured_ats == sorted(captured_ats)

    def test_ollama_unavailable_triggers_fallback(self, client, db_path):
        """Ollama down during draft embed → degraded=True, chronological fallback."""
        cs_id = "cs_ollama_down"
        _ensure_chat_session(db_path, provider_id="anthropic", chat_session_id=cs_id)
        _insert_turn_with_embedding(
            db_path, chat_session_id=cs_id,
            text="we are building the auth flow with JWTs and refresh tokens",
            embedding=_fake_vec(),
        )

        # Even though embedding exists, embed_text returns None → fallback
        with patch("memstrata.layer3.retrieval.embed_text", return_value=None):
            data = _post_rewrite(client, chat_session_id=cs_id,
                                  draft="debug the token expiry issue", provider_id="anthropic")

        assert data["degraded"] is True
        assert data["reason"] == "embeddings_pending"


# ---------------------------------------------------------------------------
# Normal path — vector search, chitchat filter, budget, temporal re-sort
# ---------------------------------------------------------------------------

class TestNormalPath:

    def test_chitchat_filter_drops_short_turns(self, client, db_path):
        """test_chitchat_filter_drops_short_turns — §8.1

        Turns with text length < 50 chars must not appear in retrieved_turns.
        """
        cs_id = "cs_chitchat"
        _ensure_chat_session(db_path, provider_id="anthropic", chat_session_id=cs_id)

        long_text = "we are building the authentication flow using JWT tokens and refresh tokens"
        short_text = "ok"  # 2 chars — below the 50-char threshold

        _insert_turn_with_embedding(db_path, chat_session_id=cs_id,
                                     text=long_text, embedding=_fake_vec(seed=1.0))
        _insert_turn_with_embedding(db_path, chat_session_id=cs_id,
                                     text=short_text, embedding=_fake_vec(seed=1.0))

        fake_embedding = _fake_vec(seed=1.0)
        with patch("memstrata.layer3.retrieval.embed_text", return_value=fake_embedding):
            data = _post_rewrite(client, chat_session_id=cs_id,
                                  draft="help me debug the auth callback", provider_id="anthropic")

        assert data["degraded"] is False
        texts = [t["text"] for t in data["retrieved_turns"]]
        assert long_text in texts, "Substantive turn must be returned"
        assert short_text not in texts, "Chitchat turn (< 50 chars) must be filtered out"

    def test_response_shape_matches_spec(self, client, db_path):
        """Response must include all §4.4 fields with correct types."""
        cs_id = "cs_shape"
        _ensure_chat_session(db_path, provider_id="openai", chat_session_id=cs_id)
        _insert_turn_with_embedding(
            db_path, chat_session_id=cs_id,
            text="we are implementing rate limiting using a token bucket algorithm",
            embedding=_fake_vec(),
        )

        with patch("memstrata.layer3.retrieval.embed_text", return_value=_fake_vec()):
            data = _post_rewrite(client, chat_session_id=cs_id,
                                  draft="how do I handle burst traffic", provider_id="openai")

        # Top-level keys
        for key in ("retrieved_turns", "token_budget_used", "token_budget_total",
                    "total_session_turns", "turns_with_embeddings", "turns_pending_embedding",
                    "turns_considered", "turns_returned", "degraded"):
            assert key in data, f"Missing key: {key}"

        assert data["degraded"] is False
        assert isinstance(data["token_budget_used"], int)
        assert data["token_budget_total"] == 1500

        if data["retrieved_turns"]:
            turn = data["retrieved_turns"][0]
            for key in ("timeline_id", "role", "text", "captured_at",
                        "similarity_score", "recency_score", "final_score", "age_human"):
                assert key in turn, f"Turn missing key: {key}"
            assert turn["similarity_score"] is not None
            assert 0.0 <= turn["final_score"] <= 1.0
            assert "Z" in turn["captured_at"], "captured_at must be ISO 8601 with Z suffix"

    def test_token_budget_skips_oversize_turns(self, client, db_path):
        """test_token_budget_skips_oversize_turns — §8.1

        A turn that alone exceeds the budget must be skipped entirely (not truncated).
        A smaller turn must still be included if it fits.
        """
        cs_id = "cs_budget"
        _ensure_chat_session(db_path, provider_id="anthropic", chat_session_id=cs_id)

        huge_text = "x" * 4000   # 1000 tokens — exceeds any reasonable budget
        small_text = "we use Python 3.12 and FastAPI for the backend service layer"

        # Give huge_text a slightly higher similarity so it ranks first in scoring
        query_vec = _fake_vec(seed=1.0)
        _insert_turn_with_embedding(db_path, chat_session_id=cs_id,
                                     text=huge_text, embedding=_fake_vec(seed=1.0))
        _insert_turn_with_embedding(db_path, chat_session_id=cs_id,
                                     text=small_text, embedding=_fake_vec(seed=0.99))

        with patch("memstrata.layer3.retrieval.embed_text", return_value=query_vec):
            data = _post_rewrite(client, chat_session_id=cs_id,
                                  draft="what stack are we using for the backend",
                                  provider_id="anthropic", budget=100)  # 100 tokens

        texts = [t["text"] for t in data["retrieved_turns"]]
        assert huge_text not in texts, "Turn exceeding budget must be skipped"
        assert small_text in texts, "Smaller fitting turn must still be included"
        # Verify text is not truncated
        for t in data["retrieved_turns"]:
            assert t["text"] in (huge_text, small_text)

    def test_temporal_supersession_output_is_chronological(self, client, db_path):
        """test_temporal_supersession — §8.1

        Even when turns are selected by relevance score, the returned list must
        be re-sorted chronologically so later turns (which supersede earlier ones)
        appear last in the injection block.
        """
        cs_id = "cs_chrono"
        _ensure_chat_session(db_path, provider_id="anthropic", chat_session_id=cs_id)

        now = datetime.now(timezone.utc)
        # Turn 1: old (3 days ago) — "Python 3.12"
        turn1_time = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        # Turn 2: new (1 hour ago) — supersedes with "Python 3.11"
        turn2_time = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

        _insert_turn_with_embedding(
            db_path, chat_session_id=cs_id,
            text="we decided to use Python 3.12 for the entire backend platform",
            embedding=_fake_vec(seed=1.0), recorded_at=turn1_time,
        )
        _insert_turn_with_embedding(
            db_path, chat_session_id=cs_id,
            text="correction: we are now using Python 3.11 due to the dependency constraints",
            embedding=_fake_vec(seed=0.98), recorded_at=turn2_time,
        )

        with patch("memstrata.layer3.retrieval.embed_text", return_value=_fake_vec(seed=1.0)):
            data = _post_rewrite(client, chat_session_id=cs_id,
                                  draft="which Python version is the project using",
                                  provider_id="anthropic")

        turns = data["retrieved_turns"]
        assert len(turns) >= 2, "Both relevant turns should be retrieved"
        captured_ats = [t["captured_at"] for t in turns]
        assert captured_ats == sorted(captured_ats), (
            "Output must be chronological so the newer superseding turn appears last"
        )
        # Confirm the older turn appears before the newer
        assert turns[0]["captured_at"] < turns[-1]["captured_at"]

    def test_scores_are_in_expected_range(self, client, db_path):
        """similarity_score, recency_score, final_score must all be in [0, 1]."""
        cs_id = "cs_scores"
        _ensure_chat_session(db_path, provider_id="anthropic", chat_session_id=cs_id)
        _insert_turn_with_embedding(
            db_path, chat_session_id=cs_id,
            text="the database layer uses SQLite with WAL mode for concurrent reads",
            embedding=_fake_vec(),
        )

        with patch("memstrata.layer3.retrieval.embed_text", return_value=_fake_vec()):
            data = _post_rewrite(client, chat_session_id=cs_id,
                                  draft="explain the database concurrency model",
                                  provider_id="anthropic")

        for turn in data["retrieved_turns"]:
            assert 0.0 <= turn["similarity_score"] <= 1.0, "similarity_score out of range"
            assert 0.0 <= turn["recency_score"] <= 1.0, "recency_score out of range"
            assert 0.0 <= turn["final_score"] <= 1.0, "final_score out of range"

    def test_turns_returned_matches_list_length(self, client, db_path):
        cs_id = "cs_counts"
        _ensure_chat_session(db_path, provider_id="anthropic", chat_session_id=cs_id)
        for i in range(5):
            _insert_turn_with_embedding(
                db_path, chat_session_id=cs_id,
                text=f"this is substantive turn number {i+1} about the auth subsystem",
                embedding=_fake_vec(seed=float(i + 1)),
            )

        with patch("memstrata.layer3.retrieval.embed_text", return_value=_fake_vec()):
            data = _post_rewrite(client, chat_session_id=cs_id,
                                  draft="tell me about the authentication subsystem",
                                  provider_id="anthropic")

        assert data["turns_returned"] == len(data["retrieved_turns"])
        assert data["total_session_turns"] == 5

    def test_cross_session_isolation(self, client, db_path):
        """Turns from another session must never appear in the response."""
        cs_a = "cs_iso_a"
        cs_b = "cs_iso_b"
        _ensure_chat_session(db_path, provider_id="anthropic", chat_session_id=cs_a)
        _ensure_chat_session(db_path, provider_id="anthropic", chat_session_id=cs_b)

        text_a = "session A uses Redis for the caching layer and rate limiting"
        text_b = "session B is about a completely different topic entirely here"

        _insert_turn_with_embedding(db_path, chat_session_id=cs_a,
                                     text=text_a, embedding=_fake_vec(seed=1.0))
        _insert_turn_with_embedding(db_path, chat_session_id=cs_b,
                                     text=text_b, embedding=_fake_vec(seed=1.0))

        with patch("memstrata.layer3.retrieval.embed_text", return_value=_fake_vec()):
            data = _post_rewrite(client, chat_session_id=cs_a,
                                  draft="explain the caching strategy for session A",
                                  provider_id="anthropic")

        texts = [t["text"] for t in data["retrieved_turns"]]
        assert text_a in texts
        assert text_b not in texts, "Turns from a different session must not bleed through"

    def test_provider_filter_respected(self, client, db_path):
        """provider_id mismatch: turns embedded under a different provider don't show."""
        cs_id = "cs_provider"
        _ensure_chat_session(db_path, provider_id="openai", chat_session_id=cs_id)
        _insert_turn_with_embedding(
            db_path, chat_session_id=cs_id,
            text="we chose FastAPI because of its async support and type annotations",
            embedding=_fake_vec(),
        )

        with patch("memstrata.layer3.retrieval.embed_text", return_value=_fake_vec()):
            # Query with the wrong provider_id
            data = _post_rewrite(client, chat_session_id=cs_id,
                                  draft="why did we choose FastAPI for the service",
                                  provider_id="anthropic")  # session is openai, not anthropic

        assert data["retrieved_turns"] == [], (
            "Wrong provider_id in query must return no turns"
        )


# ---------------------------------------------------------------------------
# Performance: §8.1 test_ingest_path_stays_fast
# ---------------------------------------------------------------------------

class TestIngestPathPerformance:
    def test_post_telemetry_session_returns_fast(self, client):
        """POST /telemetry/session must complete in < 200ms even with worker running."""
        times = []
        for i in range(5):
            start = time.perf_counter()
            resp = client.post("/telemetry/session", json={
                "session_id": f"perf_session_{i}",
                "turn_id": i,
                "project_id": "proj",
                "provider": "anthropic",
                "role": "user",
                "text": f"performance test turn number {i}",
            })
            elapsed_ms = (time.perf_counter() - start) * 1000
            assert resp.status_code == 200
            times.append(elapsed_ms)

        avg_ms = sum(times) / len(times)
        assert avg_ms < 200, (
            f"Average ingest latency {avg_ms:.1f}ms exceeds 200ms threshold. "
            f"Hard Rule 69 violation — embedding must not block the hot path."
        )


# ---------------------------------------------------------------------------
# Phase 34.4 backend — external_session_id lookup in /context/for-chat-rewrite
# ---------------------------------------------------------------------------

class TestExternalSessionIdLookup:
    """POST /context/for-chat-rewrite with external_session_id instead of chat_session_id."""

    def test_lookup_by_external_session_id_returns_history(self, client, db_path):
        """Providing external_session_id+provider_id resolves to internal ID and returns turns."""
        cs_id = "cs_ext_lookup"
        ext_id = "ext-lookup-abc"
        _ensure_chat_session(db_path, provider_id="anthropic", chat_session_id=cs_id)
        # Patch external_session_id in the chat_sessions row
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE chat_sessions SET external_session_id=? WHERE id=?", (ext_id, cs_id)
        )
        conn.commit()
        conn.close()

        _insert_turn_with_embedding(
            db_path, chat_session_id=cs_id,
            text="we use FastAPI for the REST API layer with dependency injection",
            embedding=_fake_vec(seed=1.0),
        )

        with patch("memstrata.layer3.retrieval.embed_text", return_value=_fake_vec(seed=1.0)):
            resp = client.post("/context/for-chat-rewrite", json={
                "external_session_id": ext_id,
                "provider_id": "anthropic",
                "draft_prompt": "explain the API layer architecture",
                "target_token_budget": 1500,
            })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["retrieved_turns"]) >= 1, "Should find turns via external_session_id lookup"

    def test_unknown_external_session_id_returns_no_history(self, client):
        """external_session_id not in chat_sessions → no_history, not an error."""
        resp = client.post("/context/for-chat-rewrite", json={
            "external_session_id": "ext-totally-unknown-xyz",
            "provider_id": "anthropic",
            "draft_prompt": "debug the authentication flow",
            "target_token_budget": 1500,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["reason"] == "no_history"
        assert data["retrieved_turns"] == []

    def test_missing_both_ids_returns_422(self, client):
        """Neither chat_session_id nor external_session_id provided → 422."""
        resp = client.post("/context/for-chat-rewrite", json={
            "provider_id": "anthropic",
            "draft_prompt": "debug the authentication flow",
            "target_token_budget": 1500,
        })
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Phase 34.6 — POST /telemetry/rewrite  (per-rewrite quality telemetry)
# ---------------------------------------------------------------------------

def _post_rewrite_telemetry(client, *, rewrite_id: str = "rw-test-001",
                             external_session_id: str | None = None,
                             provider_id: str = "anthropic",
                             draft_prompt_chars: int = 80,
                             retrieved_turn_count: int = 3,
                             retrieved_turn_avg_similarity: float | None = 0.79,
                             retrieved_turn_age_dist_hours: list | None = None,
                             user_confirmed: bool = True,
                             delimiter_format: str = "xml_tags",
                             token_budget_used: int | None = 1437,
                             token_budget_total: int | None = 1500,
                             degraded: bool = False,
                             degraded_reason: str | None = None) -> dict:
    payload = {
        "rewrite_id": rewrite_id,
        "provider_id": provider_id,
        "draft_prompt_chars": draft_prompt_chars,
        "retrieved_turn_count": retrieved_turn_count,
        "retrieved_turn_avg_similarity": retrieved_turn_avg_similarity,
        "retrieved_turn_age_dist_hours": retrieved_turn_age_dist_hours,
        "user_confirmed": user_confirmed,
        "delimiter_format": delimiter_format,
        "token_budget_used": token_budget_used,
        "token_budget_total": token_budget_total,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
    }
    if external_session_id is not None:
        payload["external_session_id"] = external_session_id
    resp = client.post("/telemetry/rewrite", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestRewriteTelemetryEndpoint:
    """Phase 34.6 — POST /telemetry/rewrite stores per-rewrite telemetry."""

    def test_valid_payload_stored(self, client, db_path):
        """Confirmed rewrite: all required fields persisted in rewrite_telemetry."""
        resp = _post_rewrite_telemetry(
            client,
            rewrite_id="rw-001",
            retrieved_turn_count=6,
            retrieved_turn_avg_similarity=0.79,
            retrieved_turn_age_dist_hours=[2, 8, 22, 48, 96, 240],
            user_confirmed=True,
            token_budget_used=1437,
            token_budget_total=1500,
            degraded=False,
        )
        assert resp["status"] == "ok"

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT * FROM rewrite_telemetry WHERE rewrite_id='rw-001'"
        ).fetchone()
        conn.close()

        assert row is not None, "Telemetry row not found in DB"
        col_names = [d[0] for d in conn.execute("SELECT * FROM rewrite_telemetry LIMIT 0").description] \
            if False else None
        # Re-open for column-name access
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM rewrite_telemetry WHERE rewrite_id='rw-001'"
        ).fetchone()
        conn.close()

        assert row["retrieved_turn_count"] == 6
        assert row["retrieved_turn_avg_similarity"] == pytest.approx(0.79, abs=1e-4)
        assert row["user_confirmed"] == 1
        assert row["token_budget_used"] == 1437
        assert row["token_budget_total"] == 1500
        assert row["degraded"] == 0
        assert row["degraded_reason"] is None

    def test_cancelled_rewrite_stored_with_user_confirmed_false(self, client, db_path):
        """User cancelled: user_confirmed stored as 0."""
        _post_rewrite_telemetry(client, rewrite_id="rw-cancel", user_confirmed=False)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT user_confirmed FROM rewrite_telemetry WHERE rewrite_id='rw-cancel'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["user_confirmed"] == 0

    def test_idempotent_on_same_rewrite_id(self, client, db_path):
        """Same rewrite_id posted twice: only one row stored (INSERT OR IGNORE)."""
        _post_rewrite_telemetry(client, rewrite_id="rw-dup", user_confirmed=True)
        _post_rewrite_telemetry(client, rewrite_id="rw-dup", user_confirmed=False)

        conn = sqlite3.connect(str(db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM rewrite_telemetry WHERE rewrite_id='rw-dup'"
        ).fetchone()[0]
        conn.close()
        assert count == 1, "Duplicate rewrite_id must be silently ignored"
        # First write wins (user_confirmed=True was stored)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT user_confirmed FROM rewrite_telemetry WHERE rewrite_id='rw-dup'"
        ).fetchone()
        conn.close()
        assert row["user_confirmed"] == 1

    def test_degraded_payload_stored(self, client, db_path):
        """Degraded rewrite with reason='embeddings_pending' stored correctly."""
        _post_rewrite_telemetry(
            client, rewrite_id="rw-degraded",
            degraded=True, degraded_reason="embeddings_pending",
            retrieved_turn_count=2, retrieved_turn_avg_similarity=None,
        )

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT degraded, degraded_reason FROM rewrite_telemetry WHERE rewrite_id='rw-degraded'"
        ).fetchone()
        conn.close()
        assert row["degraded"] == 1
        assert row["degraded_reason"] == "embeddings_pending"

    def test_external_session_id_resolves_to_chat_session_id(self, client, db_path):
        """When external_session_id is provided and matches a chat_session, chat_session_id is resolved."""
        cs_id = "cs_telem_resolve"
        ext_id = "ext-telem-resolve-001"
        _ensure_chat_session(db_path, provider_id="openai", chat_session_id=cs_id)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE chat_sessions SET external_session_id=? WHERE id=?", (ext_id, cs_id)
        )
        conn.commit()
        conn.close()

        _post_rewrite_telemetry(
            client, rewrite_id="rw-resolve",
            external_session_id=ext_id, provider_id="openai",
        )

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT chat_session_id, external_session_id FROM rewrite_telemetry WHERE rewrite_id='rw-resolve'"
        ).fetchone()
        conn.close()
        assert row["chat_session_id"] == cs_id, "chat_session_id must be resolved from external_session_id"
        assert row["external_session_id"] == ext_id

    def test_unknown_external_session_id_stores_null_chat_session_id(self, client, db_path):
        """Unknown external_session_id: chat_session_id stored as NULL, not an error."""
        _post_rewrite_telemetry(
            client, rewrite_id="rw-unknown-ext",
            external_session_id="ext-totally-unknown-xyz", provider_id="anthropic",
        )

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT chat_session_id FROM rewrite_telemetry WHERE rewrite_id='rw-unknown-ext'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["chat_session_id"] is None

    def test_age_distribution_stored_as_json(self, client, db_path):
        """Age distribution hours stored as JSON array, retrievable as list."""
        ages = [2, 8, 22, 48, 96, 240]
        _post_rewrite_telemetry(
            client, rewrite_id="rw-ages",
            retrieved_turn_age_dist_hours=ages,
        )

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT retrieved_turn_age_dist_hours FROM rewrite_telemetry WHERE rewrite_id='rw-ages'"
        ).fetchone()
        conn.close()
        stored = json.loads(row["retrieved_turn_age_dist_hours"])
        assert stored == ages

    def test_returns_ok_status(self, client):
        """Endpoint always returns {"status": "ok"} on success."""
        resp = _post_rewrite_telemetry(client, rewrite_id="rw-ok-check")
        assert resp == {"status": "ok"}

    def test_rewrite_telemetry_table_exists(self, client, db_path):
        """rewrite_telemetry table must be created by init_db."""
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='rewrite_telemetry'"
        ).fetchone()
        conn.close()
        assert row is not None, "rewrite_telemetry table must exist after init_db"
