"""V5.2-F — USDCAD FX rate fetch for the dashboard subscription goal.

The Stripe subscription is billed at CA$15.00/month. The dashboard
displays everything in USD (matching the OpenRouter source-of-truth
prices stored in ``provider_pricing``), so the displayed goal is
CA$15.00 converted to USD at a daily exchange rate.

Source: Bank of Canada Valet API — free, no auth, official daily noon
rate. Falls back to a baked-in constant when the API is unreachable.

Cached in process memory for 24 hours. We deliberately do NOT cache in
the DB: the V5.2-E pre-split audit classifies ``_db.py`` as Open and
keeping currency state out of the schema preserves that boundary.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from dataclasses import dataclass
from typing import Optional

_LOG = logging.getLogger(__name__)

_BOC_URL = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?recent=1"

# Static fallback used when the BoC API is unreachable. Picked at V5.2-F
# creation (2026-06-13). Erring on the "weak CAD" side keeps the goal
# slightly higher than typical realized FX — overstating the goal is
# safer than understating it (money-back-guarantee threshold).
_FALLBACK_RATE = 1.36
_FALLBACK_DATE = "2026-06-13"

_CACHE_TTL_S = 24 * 3600
_cache: FxRate | None = None
_cache_until: float = 0.0


@dataclass(frozen=True)
class FxRate:
    rate: float    # CAD per 1 USD (e.g., 1.36 means 1 USD = 1.36 CAD)
    date: str      # ISO YYYY-MM-DD of the observation
    source: str    # 'BankOfCanada' | 'fallback'


@dataclass(frozen=True)
class SubscriptionGoal:
    goal_usd: float
    fx_rate: float
    fx_date: str
    fx_source: str
    cad_price: float


# Stripe product price in CAD. Single source of truth for the dashboard
# goal computation.
SUBSCRIPTION_PRICE_CAD = 15.00


def _build_ssl_context():
    """Prefer the OS trust store (truststore) over certifi's bundle.

    Mirrors the pattern in ``openrouter_sync._build_httpx_client``: on
    machines with corporate / enterprise CAs only in the Windows store
    (not certifi), the default Python urlopen raises
    CERTIFICATE_VERIFY_FAILED. truststore reads the OS trust store
    directly. Returns None to mean "use Python defaults".
    """
    try:
        import ssl

        import truststore  # type: ignore[import]
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return None


def _fetch_boc() -> FxRate | None:
    """Fetch the latest USDCAD from BoC Valet. None on any failure."""
    try:
        req = urllib.request.Request(
            _BOC_URL,
            headers={
                "accept": "application/json",
                # BoC's WAF doesn't block default urllib, but pinning a
                # UA makes the request explicitly attributable in their
                # logs and prevents future WAF rules from blocking us.
                "user-agent": "memstrata-pro/0.6.0 (+https://memstrata.dev)",
            },
        )
        ctx = _build_ssl_context()
        kwargs = {"timeout": 5.0}
        if ctx is not None:
            kwargs["context"] = ctx
        with urllib.request.urlopen(req, **kwargs) as resp:           # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
        observations = payload.get("observations") or []
        if not observations:
            return None
        latest = observations[-1]
        date = str(latest.get("d", ""))
        cell = latest.get("FXUSDCAD") or {}
        rate_str = cell.get("v")
        if rate_str is None:
            return None
        rate = float(rate_str)
        if rate <= 0:
            return None
        return FxRate(rate=rate, date=date, source="BankOfCanada")
    except Exception as exc:                                  # noqa: BLE001
        _LOG.info("BoC USDCAD fetch failed (using fallback): %s", exc)
        return None


def get_usd_cad_rate() -> FxRate:
    """Today's USDCAD rate. Cached 24h; BoC primary, fallback secondary."""
    global _cache, _cache_until
    now = time.monotonic()
    if _cache is not None and now < _cache_until:
        return _cache
    fetched = _fetch_boc()
    if fetched is None:
        fetched = FxRate(rate=_FALLBACK_RATE, date=_FALLBACK_DATE, source="fallback")
    _cache = fetched
    _cache_until = now + _CACHE_TTL_S
    return fetched


def reset_cache() -> None:
    """Force the next ``get_usd_cad_rate()`` to re-fetch. Tests only."""
    global _cache, _cache_until
    _cache = None
    _cache_until = 0.0


def compute_subscription_goal_usd() -> SubscriptionGoal:
    """Convert ``SUBSCRIPTION_PRICE_CAD`` to USD using today's FX rate."""
    fx = get_usd_cad_rate()
    goal = round(SUBSCRIPTION_PRICE_CAD / fx.rate, 2)
    return SubscriptionGoal(
        goal_usd=goal,
        fx_rate=fx.rate,
        fx_date=fx.date,
        fx_source=fx.source,
        cad_price=SUBSCRIPTION_PRICE_CAD,
    )
