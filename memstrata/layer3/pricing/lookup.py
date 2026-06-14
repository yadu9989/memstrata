"""Phase 21.1 — pricing lookup.

Resolution order (fastest to authoritative):
  1. provider_pricing table in SQLite — populated by OpenRouter sync (live prices).
  2. Static fallback in pricing_matrix.json — used when DB is empty or unreachable.

All callers go through get_rates().  No caller should care which source won.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_MATRIX_PATH = Path(__file__).parent / "pricing_matrix.json"
_loaded_static: dict | None = None

_DATE_SUFFIX_RE = re.compile(r"-\d{8}$|-\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Rates:
    input_per_m: float
    output_per_m: float
    cache_write_per_m: float | None = None
    cache_read_per_m: float | None = None


def normalize_model(model: str) -> str:
    """Strip trailing date suffixes.

    'claude-sonnet-4-6-20251201' → 'claude-sonnet-4-6'
    'gpt-4o-2024-08-06'         → 'gpt-4o'
    """
    return _DATE_SUFFIX_RE.sub("", model)


# ---------------------------------------------------------------------------
# DB lookup (primary — live OpenRouter data)
# ---------------------------------------------------------------------------

def _get_rates_from_db(
    provider: str,
    model: str,
    conn: sqlite3.Connection,
) -> Rates | None:
    normalized = normalize_model(model)
    for m in (model, normalized):
        try:
            row = conn.execute(
                """
                SELECT input_per_m, output_per_m, cache_write_per_m, cache_read_per_m
                FROM provider_pricing
                WHERE provider = ? AND model = ?
                """,
                (provider, m),
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # table not yet created

        if row:
            return Rates(
                input_per_m=float(row[0]),
                output_per_m=float(row[1]),
                cache_write_per_m=float(row[2]) if row[2] is not None else None,
                cache_read_per_m=float(row[3]) if row[3] is not None else None,
            )
    return None


# ---------------------------------------------------------------------------
# Static fallback
# ---------------------------------------------------------------------------

def _load_static() -> dict:
    global _loaded_static
    if _loaded_static is None:
        _loaded_static = json.loads(_MATRIX_PATH.read_text(encoding="utf-8"))
    return _loaded_static


def _get_rates_from_static(provider: str, model: str) -> Rates | None:
    matrix = _load_static()
    providers = matrix.get("providers", {})
    if provider not in providers:
        return None
    models = providers[provider]
    normalized = normalize_model(model)
    entry = models.get(model) or models.get(normalized)
    if entry is None:
        return None
    return Rates(
        input_per_m=float(entry["input_per_m"]),
        output_per_m=float(entry["output_per_m"]),
        cache_write_per_m=float(entry["cache_write_per_m"]) if "cache_write_per_m" in entry else None,
        cache_read_per_m=float(entry["cache_read_per_m"]) if "cache_read_per_m" in entry else None,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_rates(
    provider: str,
    model: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> Rates | None:
    """Return Rates for (provider, model) or None if unknown.

    Tries live DB first (OpenRouter data), then falls back to the bundled
    static pricing_matrix.json.  Caller is not required to pass conn —
    passing None skips the DB path and goes straight to static.
    """
    if conn is not None:
        rates = _get_rates_from_db(provider, model, conn)
        if rates is not None:
            return rates

    return _get_rates_from_static(provider, model)


def compute_input_savings_usd(
    baseline_tokens: int,
    actual_tokens: int,
    rates: Rates,
) -> float:
    """Dollar savings from injecting a smaller context block than the naive baseline."""
    saved = max(0, baseline_tokens - actual_tokens)
    return saved * rates.input_per_m / 1_000_000


def compute_cache_savings_usd(
    cached_tokens: int,
    rates: Rates,
) -> float:
    """Dollar savings from provider KV-cache hits (cache_read cheaper than input)."""
    if rates.cache_read_per_m is None:
        return 0.0
    standard = cached_tokens * rates.input_per_m / 1_000_000
    actual = cached_tokens * rates.cache_read_per_m / 1_000_000
    return max(0.0, standard - actual)


def compute_output_savings_usd(
    baseline_output_tokens: int,
    actual_output_tokens: int,
    rates: Rates,
) -> float:
    """Dollar savings from generating a shorter output than the cohort baseline.

    MemStrata's injected context lets the model answer more concisely (it
    doesn't have to restate background). When actual_output < baseline (the
    project's cohort-measured average output per turn), the user saves
    (baseline - actual) * output_per_m.

    Per V5.3 Hard Rule 60, this is *measured* savings against the cohort
    baseline, not an "imputed" counterfactual — only credit-eligible when
    cohort baseline has actually closed (caller's responsibility to gate).
    """
    saved = max(0, baseline_output_tokens - actual_output_tokens)
    return saved * rates.output_per_m / 1_000_000
