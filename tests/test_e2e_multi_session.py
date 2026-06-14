"""Phase 34: Full Multi-Session End-to-End Sign-Off.

Verifies token-saving compounding aggregation across all simulated chat
environments (8 URL-session providers) with split harness session IDs.

Key invariants validated:
  - All 8 provider external session ID formats accepted → chat_sessions rows created
  - tokens_saved_est = avg_context_overhead × cache_hit_turns (per session)
  - Split harness sessions for the same external_session_id aggregate to one chat_sessions row
  - total_tokens_saved_est sums correctly across all sessions
  - Cross-provider isolation: same external_session_id under different providers → separate rows
  - Edge cases: no cache hits → 0, no baseline data → 0 graceful fallback
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
    monkeypatch.setenv("ML_DB_PATH", str(tmp_path / "e2e_core.db"))


@pytest.fixture
def client(isolated_db):
    from memstrata.layer3.api_server import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_conn(tmp_path, isolated_db):
    import os
    path = os.environ["ML_DB_PATH"]
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Turn posting helpers
# ---------------------------------------------------------------------------

_TURN_COUNTER: dict[str, int] = {}


def _post_turn(
    client: TestClient,
    *,
    session_id: str,
    project_id: str = "proj_e2e",
    external_session_id: str | None = None,
    provider: str | None = None,
    actual_input_tokens: int = 1000,
    actual_output_tokens: int = 200,
    baseline_no_context: int | None = None,
    injected: int = 0,
    cache_hit_estimated: int = 0,
) -> dict:
    key = f"{session_id}"
    _TURN_COUNTER[key] = _TURN_COUNTER.get(key, 0) + 1
    turn_id = _TURN_COUNTER[key]

    payload: dict = {
        "session_id": session_id,
        "turn_id": turn_id,
        "project_id": project_id,
        "actual_input_tokens": actual_input_tokens,
        "actual_output_tokens": actual_output_tokens,
        "injected": injected,
        "cache_hit_estimated": cache_hit_estimated,
    }
    if baseline_no_context is not None:
        payload["baseline_no_context"] = baseline_no_context
    if external_session_id is not None:
        payload["external_session_id"] = external_session_id
    if provider is not None:
        payload["provider"] = provider

    r = client.post("/telemetry/session", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _sessions(client: TestClient) -> list[dict]:
    """Return only browser-captured chat sessions (chat_session_id is not None)."""
    r = client.get("/api/dashboard/sessions")
    assert r.status_code == 200, r.text
    data = r.json()
    return [s for s in data.get("sessions", []) if s.get("chat_session_id") is not None]


def _state(client: TestClient) -> dict:
    r = client.get("/api/dashboard/state")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Provider external session ID format coverage
# ---------------------------------------------------------------------------

# Real-world provider session ID formats from provider_hints.json
_PROVIDER_SESSION_IDS = [
    # (provider_id, external_session_id, description)
    ("anthropic", "3f4e5d6c-7b8a-9012-cdef-3456789abcde", "Claude UUID v4"),
    ("openai",    "673f2a9c-dd7b-4a6f-8e1c-0b2a3c4d5e6f", "ChatGPT UUID v4"),
    ("google",    "aabbccdd-eeff-0011-2233-445566778899", "Gemini UUID v4"),
    ("deepseek",  "a1b2c3d4e5f6a7b8",                    "DeepSeek hex-16"),
    ("xai",       "msg-9z8y7x6w5v4u3t2",                 "Grok alphanumeric+hyphen"),
    ("mistral",   "4fb8pRzqKqGPnmSy",                    "Mistral alphanumeric"),
    ("meta",      "123456789012345",                      "Meta.ai numeric"),
    ("perplexity","pplx-f1e2d3c4b5a6978",               "Perplexity alphanumeric+hyphen"),
]


class TestAllProviderFormatsAccepted:
    """All 8 URL-session providers' ID formats create chat_sessions rows."""

    def test_all_eight_provider_formats_accepted(self, client, db_conn):
        for prov, ext_id, _ in _PROVIDER_SESSION_IDS:
            _post_turn(
                client,
                session_id=f"hs-{prov}",
                external_session_id=ext_id,
                provider=prov,
            )

        rows = db_conn.execute("SELECT provider_id, external_session_id FROM chat_sessions ORDER BY provider_id").fetchall()
        assert len(rows) == 8

        found_providers = {r["provider_id"] for r in rows}
        expected = {p for p, _, _ in _PROVIDER_SESSION_IDS}
        assert found_providers == expected

    def test_each_provider_session_appears_in_dashboard(self, client):
        for prov, ext_id, _ in _PROVIDER_SESSION_IDS:
            _post_turn(
                client,
                session_id=f"hs2-{prov}",
                external_session_id=ext_id,
                provider=prov,
                actual_input_tokens=500,
            )

        sessions = _sessions(client)
        assert len(sessions) == 8

    def test_turn_count_increments_per_provider(self, client, db_conn):
        prov, ext_id, _ = _PROVIDER_SESSION_IDS[0]  # anthropic
        for _ in range(3):
            _post_turn(client, session_id="hs-tc", external_session_id=ext_id, provider=prov)

        row = db_conn.execute(
            "SELECT turn_count FROM chat_sessions WHERE provider_id = ? AND external_session_id = ?",
            (prov, ext_id),
        ).fetchone()
        assert row["turn_count"] == 3


# ---------------------------------------------------------------------------
# Simple 2-turn savings verification
# ---------------------------------------------------------------------------

class TestSimpleTwoTurnSavings:
    """1 injection turn + 1 cache hit → tokens_saved_est = overhead."""

    def test_overhead_times_one_cache_hit(self, client):
        # Turn 1: injection, establishes overhead = 5000 - 600 = 4400
        _post_turn(
            client,
            session_id="hs-simple",
            external_session_id="simple-abc123",
            provider="openai",
            actual_input_tokens=5000,
            baseline_no_context=600,
            injected=1,
            cache_hit_estimated=0,
        )
        # Turn 2: cache hit
        _post_turn(
            client,
            session_id="hs-simple",
            external_session_id="simple-abc123",
            provider="openai",
            actual_input_tokens=5000,
            cache_hit_estimated=1,
        )

        sessions = _sessions(client)
        assert len(sessions) == 1
        assert sessions[0]["tokens_saved_est"] == 4400

    def test_no_cache_hit_means_zero_savings(self, client):
        # 1 injection + 0 cache hits
        _post_turn(
            client,
            session_id="hs-nocache",
            external_session_id="nocache-xyz",
            provider="openai",
            actual_input_tokens=5000,
            baseline_no_context=600,
            injected=1,
            cache_hit_estimated=0,
        )

        sessions = _sessions(client)
        assert len(sessions) == 1
        assert sessions[0]["tokens_saved_est"] == 0


# ---------------------------------------------------------------------------
# Compounding savings: 1 injection + multiple cache hits
# ---------------------------------------------------------------------------

class TestCompoundingSavings:
    """overhead=5000 per injection turn; 3 cache hits → saved=15000."""

    def test_one_injection_three_cache_hits(self, client):
        # overhead = 6000 - 1000 = 5000
        _post_turn(
            client,
            session_id="hs-comp",
            external_session_id="comp-session1",
            provider="anthropic",
            actual_input_tokens=6000,
            baseline_no_context=1000,
            injected=1,
            cache_hit_estimated=0,
        )
        for _ in range(3):
            _post_turn(
                client,
                session_id="hs-comp",
                external_session_id="comp-session1",
                provider="anthropic",
                actual_input_tokens=6000,
                cache_hit_estimated=1,
            )

        sessions = _sessions(client)
        assert len(sessions) == 1
        assert sessions[0]["tokens_saved_est"] == 15000

    def test_two_injections_five_cache_hits(self, client):
        # overhead = (8000-1000 + 9000-1000) / 2 = (7000+8000)/2 = 7500
        # saved = 7500 × 5 = 37500
        _post_turn(
            client,
            session_id="hs-multi-inj",
            external_session_id="multi-inj-sess",
            provider="anthropic",
            actual_input_tokens=8000,
            baseline_no_context=1000,
            injected=1,
            cache_hit_estimated=0,
        )
        _post_turn(
            client,
            session_id="hs-multi-inj",
            external_session_id="multi-inj-sess",
            provider="anthropic",
            actual_input_tokens=9000,
            baseline_no_context=1000,
            injected=1,
            cache_hit_estimated=0,
        )
        for _ in range(5):
            _post_turn(
                client,
                session_id="hs-multi-inj",
                external_session_id="multi-inj-sess",
                provider="anthropic",
                actual_input_tokens=8500,
                cache_hit_estimated=1,
            )

        sessions = _sessions(client)
        assert len(sessions) == 1
        assert sessions[0]["tokens_saved_est"] == 37500


# ---------------------------------------------------------------------------
# Split session ID aggregation
# ---------------------------------------------------------------------------

class TestSplitSessionAggregation:
    """Two harness session_ids for the same external_session_id → one chat_sessions row."""

    def test_split_harness_sessions_produce_one_chat_session(self, client, db_conn):
        # Harness session A: injection turn, overhead = 5000 - 600 = 4400
        _post_turn(
            client,
            session_id="harness-A",
            external_session_id="shared-ext-id",
            provider="openai",
            actual_input_tokens=5000,
            baseline_no_context=600,
            injected=1,
            cache_hit_estimated=0,
        )
        # Harness session B (new harness session, same browser chat): cache hit
        _post_turn(
            client,
            session_id="harness-B",
            external_session_id="shared-ext-id",
            provider="openai",
            actual_input_tokens=5000,
            cache_hit_estimated=1,
        )

        rows = db_conn.execute("SELECT * FROM chat_sessions").fetchall()
        assert len(rows) == 1
        assert rows[0]["turn_count"] == 2

    def test_split_sessions_savings_aggregate_correctly(self, client):
        # overhead from session A = 5000 - 600 = 4400; 1 cache hit from B → saved = 4400
        _post_turn(
            client,
            session_id="sA",
            external_session_id="split-savings",
            provider="openai",
            actual_input_tokens=5000,
            baseline_no_context=600,
            injected=1,
            cache_hit_estimated=0,
        )
        _post_turn(
            client,
            session_id="sB",
            external_session_id="split-savings",
            provider="openai",
            actual_input_tokens=5000,
            cache_hit_estimated=1,
        )
        # Also add another injection from a third harness session: overhead = 5000 - 600 = 4400
        # avg_overhead is still 4400 (both injection turns have same overhead)
        # 2 cache hits total → saved = 4400 × 2 = 8800... wait, total cache hits = 1 (sB) + 1 below = 2
        # But only one cache hit posted above. Let's add one more from sC.
        _post_turn(
            client,
            session_id="sC",
            external_session_id="split-savings",
            provider="openai",
            actual_input_tokens=5000,
            baseline_no_context=600,
            injected=1,
            cache_hit_estimated=0,
        )
        # avg_overhead = (4400+4400)/2 = 4400; cache_hits=1 → saved = 4400×1 = 4400
        # Actually only 1 cache hit (from sB). So saved = 4400×1 = 4400
        sessions = _sessions(client)
        assert len(sessions) == 1
        assert sessions[0]["tokens_saved_est"] == 4400

    def test_split_sessions_turn_count_totals(self, client, db_conn):
        for harness_id in ["h1", "h2", "h3"]:
            _post_turn(
                client,
                session_id=harness_id,
                external_session_id="multi-harness-chat",
                provider="google",
            )

        row = db_conn.execute(
            "SELECT turn_count FROM chat_sessions WHERE external_session_id = ?",
            ("multi-harness-chat",),
        ).fetchone()
        assert row["turn_count"] == 3


# ---------------------------------------------------------------------------
# Cross-provider independence
# ---------------------------------------------------------------------------

class TestCrossProviderIndependence:
    """Same external_session_id under different providers → separate chat_sessions rows."""

    def test_same_ext_id_different_providers_creates_two_rows(self, client, db_conn):
        _post_turn(
            client,
            session_id="hs-cp1",
            external_session_id="same-id-123",
            provider="openai",
        )
        _post_turn(
            client,
            session_id="hs-cp2",
            external_session_id="same-id-123",
            provider="anthropic",
        )

        rows = db_conn.execute(
            "SELECT provider_id FROM chat_sessions WHERE external_session_id = ? ORDER BY provider_id",
            ("same-id-123",),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["provider_id"] == "anthropic"
        assert rows[1]["provider_id"] == "openai"

    def test_savings_computed_independently_per_provider(self, client):
        # OpenAI session: overhead=4000, 2 cache hits → saved=8000
        _post_turn(
            client, session_id="hs-oai",
            external_session_id="cross-prov-ext",
            provider="openai",
            actual_input_tokens=5000, baseline_no_context=1000,
            injected=1, cache_hit_estimated=0,
        )
        for _ in range(2):
            _post_turn(
                client, session_id="hs-oai",
                external_session_id="cross-prov-ext",
                provider="openai",
                actual_input_tokens=5000, cache_hit_estimated=1,
            )

        # Anthropic session: overhead=3000, 1 cache hit → saved=3000
        _post_turn(
            client, session_id="hs-ant",
            external_session_id="cross-prov-ext",
            provider="anthropic",
            actual_input_tokens=4000, baseline_no_context=1000,
            injected=1, cache_hit_estimated=0,
        )
        _post_turn(
            client, session_id="hs-ant",
            external_session_id="cross-prov-ext",
            provider="anthropic",
            actual_input_tokens=4000, cache_hit_estimated=1,
        )

        sessions = _sessions(client)
        by_provider: dict[str, int] = {}
        for s in sessions:
            by_provider[s["provider_id"]] = s["tokens_saved_est"]

        assert by_provider["openai"] == 8000
        assert by_provider["anthropic"] == 3000


# ---------------------------------------------------------------------------
# Global total_tokens_saved_est
# ---------------------------------------------------------------------------

class TestGlobalTotalSavings:
    """total_tokens_saved_est in dashboard state sums across all sessions."""

    def test_total_is_sum_of_all_session_savings(self, client):
        # Session 1 (openai): overhead=4000, 2 hits → 8000
        _post_turn(
            client, session_id="g1",
            external_session_id="global-sess-1",
            provider="openai",
            actual_input_tokens=5000, baseline_no_context=1000,
            injected=1, cache_hit_estimated=0,
        )
        for _ in range(2):
            _post_turn(
                client, session_id="g1",
                external_session_id="global-sess-1",
                provider="openai",
                actual_input_tokens=5000, cache_hit_estimated=1,
            )

        # Session 2 (anthropic): overhead=6000, 1 hit → 6000
        _post_turn(
            client, session_id="g2",
            external_session_id="global-sess-2",
            provider="anthropic",
            actual_input_tokens=7000, baseline_no_context=1000,
            injected=1, cache_hit_estimated=0,
        )
        _post_turn(
            client, session_id="g2",
            external_session_id="global-sess-2",
            provider="anthropic",
            actual_input_tokens=7000, cache_hit_estimated=1,
        )

        state = _state(client)
        # total = 8000 + 6000 = 14000
        assert state["total_tokens_saved_est"] == 14000

    def test_empty_db_total_is_zero(self, client):
        state = _state(client)
        assert state["total_tokens_saved_est"] == 0

    def test_sessions_with_only_injection_contribute_zero(self, client):
        # No cache hits anywhere → total stays 0
        _post_turn(
            client, session_id="g-nohit",
            external_session_id="no-hits-ext",
            provider="openai",
            actual_input_tokens=5000, baseline_no_context=1000,
            injected=1, cache_hit_estimated=0,
        )

        state = _state(client)
        assert state["total_tokens_saved_est"] == 0


# ---------------------------------------------------------------------------
# Edge cases: missing baseline data
# ---------------------------------------------------------------------------

class TestMissingBaselineGracefulFallback:
    """Sessions without baseline_no_context on injection turns → tokens_saved_est=0."""

    def test_no_baseline_means_zero_savings(self, client):
        # injected=1 but no baseline_no_context → cannot compute overhead → saved=0
        _post_turn(
            client, session_id="nb",
            external_session_id="no-baseline",
            provider="openai",
            actual_input_tokens=5000,
            injected=1,
            cache_hit_estimated=0,
        )
        _post_turn(
            client, session_id="nb",
            external_session_id="no-baseline",
            provider="openai",
            actual_input_tokens=5000,
            cache_hit_estimated=1,
        )

        sessions = _sessions(client)
        assert len(sessions) == 1
        assert sessions[0]["tokens_saved_est"] == 0

    def test_partial_baseline_uses_available_data(self, client):
        # 2 injection turns: one with baseline (overhead=4000), one without
        # avg overhead = 4000 / 1 qualifying turn = 4000; 1 cache hit → saved=4000
        _post_turn(
            client, session_id="pb",
            external_session_id="partial-baseline",
            provider="openai",
            actual_input_tokens=5000, baseline_no_context=1000,
            injected=1, cache_hit_estimated=0,
        )
        _post_turn(
            client, session_id="pb",
            external_session_id="partial-baseline",
            provider="openai",
            actual_input_tokens=5000,  # no baseline_no_context
            injected=1, cache_hit_estimated=0,
        )
        _post_turn(
            client, session_id="pb",
            external_session_id="partial-baseline",
            provider="openai",
            actual_input_tokens=5000,
            cache_hit_estimated=1,
        )

        sessions = _sessions(client)
        assert len(sessions) == 1
        assert sessions[0]["tokens_saved_est"] == 4000


# ---------------------------------------------------------------------------
# Full scenario: realistic multi-provider workload
# ---------------------------------------------------------------------------

class TestFullMultiProviderWorkload:
    """Simulates a day's worth of usage across 4 providers; verifies total."""

    def test_full_workload_totals(self, client):
        scenarios = [
            # (provider, ext_id, overhead, n_cache_hits)
            ("openai",    "chatgpt-work-1",     3000, 4),   # saved=12000
            ("anthropic", "claude-work-1",      5000, 2),   # saved=10000
            ("google",    "gemini-work-1",      2000, 5),   # saved=10000
            ("mistral",   "mistral-work-1",     4000, 3),   # saved=12000
        ]
        expected_total = 12000 + 10000 + 10000 + 12000  # 44000

        for provider, ext_id, overhead, n_hits in scenarios:
            # baseline = 1000, input_tokens = 1000 + overhead
            input_tokens = 1000 + overhead
            _post_turn(
                client, session_id=f"ws-{provider}",
                external_session_id=ext_id,
                provider=provider,
                actual_input_tokens=input_tokens,
                baseline_no_context=1000,
                injected=1,
                cache_hit_estimated=0,
            )
            for _ in range(n_hits):
                _post_turn(
                    client, session_id=f"ws-{provider}",
                    external_session_id=ext_id,
                    provider=provider,
                    actual_input_tokens=input_tokens,
                    cache_hit_estimated=1,
                )

        state = _state(client)
        assert state["total_tokens_saved_est"] == expected_total
        assert state["sessions"] == 4

    def test_harness_only_turns_excluded_from_chat_sessions(self, client):
        # Post some harness-only turns (no external_session_id)
        for i in range(3):
            _post_turn(client, session_id=f"harness-only-{i}")

        # Post one real chat session
        _post_turn(
            client, session_id="real-chat",
            external_session_id="real-ext-id",
            provider="openai",
        )

        state = _state(client)
        # dashboard_state only counts chat sessions (INNER JOIN chat_sessions)
        assert state["sessions"] == 1
