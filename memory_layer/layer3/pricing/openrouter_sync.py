"""Phase 21 — Dynamic pricing sync from OpenRouter.

OpenRouter exposes live model pricing at:
  GET https://openrouter.ai/api/v1/models
  → [{"id": "anthropic/claude-sonnet-4-6", "pricing": {"prompt": "0.000003", "completion": "0.000015"}, ...}, ...]

Pricing fields are in USD per token (not per million).  We store them as
per-million in provider_pricing for consistency with the rest of the system.

Provider mapping: OpenRouter uses 'provider/model' slug format.  We strip the
provider prefix and store with the normalised provider name.  e.g.
  "anthropic/claude-sonnet-4-6" → provider="anthropic", model="claude-sonnet-4-6"

Cache pricing: OpenRouter reports cache_read_price and cache_write_price as
separate fields when supported.  Falls back to None when absent.

The sync runs on startup and then every 24 hours via a background task started
in api_server.py lifespan.  Failures are logged and ignored — the fallback
static JSON is always available.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import httpx

_logger = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
_SYNC_INTERVAL_S = 24 * 3600  # re-sync every 24 h

# Provider slug → canonical provider name used in the rest of the system.
_SLUG_TO_PROVIDER: dict[str, str] = {
    "anthropic":  "anthropic",
    "openai":     "openai",
    "google":     "google",
    "deepseek":   "deepseek",
    "mistralai":  "mistral",
    "meta-llama": "meta",
    "x-ai":       "xai",
    "perplexity": "perplexity",
    "cohere":     "cohere",
}


def _parse_openrouter_models(data: list[dict]) -> list[dict]:
    """Convert OpenRouter model list to rows for provider_pricing."""
    rows = []
    now = datetime.now(timezone.utc).isoformat()

    for entry in data:
        model_id: str = entry.get("id", "")
        if "/" not in model_id:
            continue

        slug, model_name = model_id.split("/", 1)
        provider = _SLUG_TO_PROVIDER.get(slug)
        if provider is None:
            continue  # skip unmapped providers

        pricing = entry.get("pricing") or {}

        def _to_per_m(val: object) -> Optional[float]:
            try:
                f = float(val)  # type: ignore[arg-type]
                return round(f * 1_000_000, 6) if f > 0 else None
            except (TypeError, ValueError):
                return None

        input_per_m  = _to_per_m(pricing.get("prompt"))
        output_per_m = _to_per_m(pricing.get("completion"))

        if input_per_m is None or output_per_m is None:
            continue  # can't compute savings without at least these two

        rows.append({
            "provider":         provider,
            "model":            model_name,
            "input_per_m":      input_per_m,
            "output_per_m":     output_per_m,
            "cache_write_per_m": _to_per_m(pricing.get("image_generation")),  # n/a, keep None
            "cache_read_per_m":  _to_per_m(pricing.get("cache_read")),
            "fetched_at":        now,
        })

    return rows


def sync_from_openrouter_sync(conn: sqlite3.Connection) -> int:
    """Blocking fetch from OpenRouter and upsert into provider_pricing.

    Returns the number of rows upserted.  Raises on HTTP or parse errors so
    the caller can log and fall back gracefully.
    """
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(_OPENROUTER_URL)
    resp.raise_for_status()

    payload = resp.json()
    models_list: list[dict] = payload.get("data", payload) if isinstance(payload, dict) else payload

    rows = _parse_openrouter_models(models_list)
    if not rows:
        raise ValueError("OpenRouter returned 0 usable pricing rows")

    conn.executemany(
        """
        INSERT INTO provider_pricing
               (provider, model, input_per_m, output_per_m,
                cache_write_per_m, cache_read_per_m, fetched_at)
        VALUES (:provider, :model, :input_per_m, :output_per_m,
                :cache_write_per_m, :cache_read_per_m, :fetched_at)
        ON CONFLICT(provider, model) DO UPDATE SET
            input_per_m       = excluded.input_per_m,
            output_per_m      = excluded.output_per_m,
            cache_write_per_m = excluded.cache_write_per_m,
            cache_read_per_m  = excluded.cache_read_per_m,
            fetched_at        = excluded.fetched_at
        """,
        rows,
    )
    conn.commit()
    _logger.info("OpenRouter pricing sync: upserted %d models", len(rows))
    return len(rows)


async def sync_loop(conn_factory) -> None:
    """Background coroutine: sync on startup then every 24 h.

    conn_factory() must return a fresh sqlite3.Connection each call.
    """
    while True:
        try:
            conn = conn_factory()
            try:
                count = await asyncio.to_thread(sync_from_openrouter_sync, conn)
                _logger.info("Pricing sync: %d providers updated from OpenRouter", count)
            finally:
                conn.close()
        except Exception as exc:
            _logger.warning(
                "OpenRouter pricing sync failed (will retry in 24 h): %s", exc
            )
        await asyncio.sleep(_SYNC_INTERVAL_S)
