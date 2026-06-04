"""Phase 33 — two-tier billing feature gate.

Maps plan → enabled feature flags. Consumed by:
  - api_server.py  → GET /license/plan-features, GET /license/current-plan
  - harness MemoryLayerClient → is_feature_active('harness')
  - Any code that must behave differently for free/lite/pro/team users

Hard Rule 64: callers must fail-open when the DB or network is unavailable.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Sequence

# Canonical feature flag strings (matches FeatureFlag union in snippets_V5_4_ADDENDUM.ts)
_PLAN_FEATURES: dict[str, list[str]] = {
    'free':  ['mcp_server', 'local_dashboard'],
    'trial': ['mcp_server', 'local_dashboard', 'browser_ext', 'harness', 'vscode_ext', 'money_tab'],
    'lite':  ['mcp_server', 'local_dashboard', 'browser_ext', 'money_tab_chat_only'],
    'pro':   ['mcp_server', 'local_dashboard', 'browser_ext', 'harness', 'vscode_ext', 'money_tab'],
    'team':  ['mcp_server', 'local_dashboard', 'browser_ext', 'harness', 'vscode_ext', 'money_tab',
              'team_sync', 'shared_dashboard'],
}

_VALID_PLANS: frozenset[str] = frozenset(_PLAN_FEATURES)


def get_current_plan(conn: sqlite3.Connection) -> str:
    """Read the current plan from the settings table. Defaults to 'trial'."""
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'current_plan'"
        ).fetchone()
        if row:
            plan = row[0] if isinstance(row, tuple) else row['value']
            if plan in _VALID_PLANS:
                return plan
    except sqlite3.Error:
        pass
    return 'trial'


def get_plan_features(conn: sqlite3.Connection, plan: str | None = None) -> list[str]:
    """Return the feature list for *plan* (or the current plan if None)."""
    if plan is None:
        plan = get_current_plan(conn)
    return list(_PLAN_FEATURES.get(plan, _PLAN_FEATURES['free']))


def is_feature_active(feature: str, conn: sqlite3.Connection) -> bool:
    """Return True when *feature* is enabled for the current plan."""
    return feature in get_plan_features(conn)


def set_current_plan(conn: sqlite3.Connection, plan: str) -> None:
    """Persist *plan* as the active plan. Raises ValueError for unknown plans."""
    if plan not in _VALID_PLANS:
        raise ValueError(f"Unknown plan {plan!r}. Valid: {sorted(_VALID_PLANS)}")
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('current_plan', ?)",
        (plan,),
    )
    conn.commit()


def all_plan_names() -> Sequence[str]:
    """Return the ordered list of valid plan names."""
    return list(_PLAN_FEATURES)
