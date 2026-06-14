"""Phase 34.3 — Pure helpers for /context/for-chat-rewrite.

All functions except embed_text are pure (no DB / network I/O) so they can be
unit-tested in isolation without FastAPI or sqlite-vec.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from memstrata.layer3._db import parse_recorded_at

_logger = logging.getLogger(__name__)

_OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"
_EMBED_MODEL = "nomic-embed-text"

# §4.2
RECENCY_HALF_LIFE_SECONDS: float = 3600 * 24 * 3  # 3 days

# Top-K candidates retrieved from vector search before scoring + budget trim.
CANDIDATE_K = 20


# ---------------------------------------------------------------------------
# Ollama embedding (the only allowed synchronous embed call per §3.2)
# ---------------------------------------------------------------------------

def embed_text(text: str) -> list[float] | None:
    """Embed *text* synchronously via Ollama nomic-embed-text.

    Returns the 768-dim float list, or None on any failure.
    Callers must handle None gracefully (degraded mode per §5.2).
    """
    try:
        resp = requests.post(
            _OLLAMA_EMBED_URL,
            json={"model": _EMBED_MODEL, "input": [text]},
            timeout=10.0,
        )
    except Exception as exc:
        _logger.warning("embed_text: request failed: %s", exc)
        return None

    if not resp.ok:
        _logger.warning("embed_text: Ollama %d: %s", resp.status_code, resp.text[:200])
        return None

    try:
        data = resp.json()
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or not embeddings or not embeddings[0]:
            _logger.warning("embed_text: unexpected response shape: %s", str(data)[:200])
            return None
        return embeddings[0]
    except Exception as exc:
        _logger.warning("embed_text: parse error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Scoring (§4.2)
# ---------------------------------------------------------------------------

def compute_final_score(
    similarity: float,
    recorded_at_str: str,
    now: datetime,
    alpha: float = 0.7,
) -> tuple[float, float, float]:
    """Return (final_score, similarity_component, recency_component).

    similarity: cosine similarity in [0, 1] (1 = identical vectors).
    alpha=0.7 means similarity dominates; recency provides a tiebreak.
    """
    captured_at = parse_recorded_at(recorded_at_str)
    age_seconds = max(0.0, (now - captured_at).total_seconds())
    recency_score = 0.5 ** (age_seconds / RECENCY_HALF_LIFE_SECONDS)
    final_score = (alpha * similarity) + ((1.0 - alpha) * recency_score)
    return final_score, similarity, recency_score


# ---------------------------------------------------------------------------
# Token budget (§4.3)
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Approximate token count: 4 chars ≈ 1 token."""
    return len(text) // 4


def fit_to_budget(scored_turns: list[dict], token_budget: int) -> list[dict]:
    """Greedy-fill to *token_budget* sorted by final_score descending.

    Skips turns that would exceed the remaining budget — never truncates a turn
    (Hard Rule 68: a partial turn changes meaning; skip entirely instead).
    """
    selected: list[dict] = []
    used = 0
    for turn in sorted(scored_turns, key=lambda t: -(t.get("final_score") or 0.0)):
        cost = estimate_tokens(turn["text"])
        if used + cost <= token_budget:
            selected.append(turn)
            used += cost
    return selected


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def age_human(dt: datetime, now: datetime) -> str:
    """Human-readable age: 'just now', '3 hours ago', '5 days ago', etc."""
    age_s = max(0.0, (now - dt).total_seconds())
    if age_s < 60:
        return "just now"
    if age_s < 3600:
        m = int(age_s / 60)
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if age_s < 86400:
        h = int(age_s / 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = int(age_s / 86400)
    return f"{d} day{'s' if d != 1 else ''} ago"


def recorded_at_to_iso(recorded_at_str: str) -> str:
    """Convert DB TEXT timestamp to ISO 8601 UTC string ('…Z' suffix)."""
    dt = parse_recorded_at(recorded_at_str)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
