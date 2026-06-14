"""Tests for V5.2-A Phase 35.4 — resource policy decisions.

OS-level calls (SetPriorityClass, ioreg, etc.) are out of scope here.
We exercise the *decision* logic with injected detectors.
"""
from __future__ import annotations

import os

import pytest

from memstrata.layer3.ingestion.resource_policy import (
    DEFAULT_CONCURRENT_EMBEDDINGS_CAP,
    BatteryState,
    ResourcePolicy,
)


def _battery(plugged: bool) -> BatteryState:
    return BatteryState(on_battery_power=not plugged)


# ── Battery policy ─────────────────────────────────────────────────────

class TestBatteryPolicy:
    def test_pauses_on_battery(self):
        p = ResourcePolicy(
            battery_detector=lambda: _battery(plugged=False),
            typing_idle_detector=lambda: None,
        )
        paused, reason = p.should_pause_embedding()
        assert paused
        assert reason == "on-battery"

    def test_does_not_pause_on_ac(self):
        p = ResourcePolicy(
            battery_detector=lambda: _battery(plugged=True),
            typing_idle_detector=lambda: None,
        )
        paused, _ = p.should_pause_embedding()
        assert not paused

    def test_battery_policy_disabled(self):
        p = ResourcePolicy(
            pause_on_battery=False,
            battery_detector=lambda: _battery(plugged=False),
            typing_idle_detector=lambda: None,
        )
        paused, _ = p.should_pause_embedding()
        assert not paused

    def test_undetectable_battery_does_not_pause(self):
        p = ResourcePolicy(
            battery_detector=lambda: BatteryState(False, None, detectable=False),
            typing_idle_detector=lambda: None,
        )
        paused, _ = p.should_pause_embedding()
        assert not paused


# ── Typing/idle policy ─────────────────────────────────────────────────

class TestTypingPolicy:
    def test_pauses_when_user_recently_active(self):
        p = ResourcePolicy(
            typing_idle_seconds=30.0,
            battery_detector=lambda: _battery(True),
            typing_idle_detector=lambda: 5.0,    # 5s idle = recently active
        )
        paused, reason = p.should_pause_embedding()
        assert paused
        assert "recent-input" in reason

    def test_no_pause_when_user_idle(self):
        p = ResourcePolicy(
            typing_idle_seconds=30.0,
            battery_detector=lambda: _battery(True),
            typing_idle_detector=lambda: 120.0,   # 2 minutes idle
        )
        paused, _ = p.should_pause_embedding()
        assert not paused

    def test_no_pause_when_idle_detection_unsupported(self):
        """None from the detector means platform can't tell; per the
        spec we don't pause in that case — over-indexing is preferable
        to never indexing on a platform we don't recognize."""
        p = ResourcePolicy(
            battery_detector=lambda: _battery(True),
            typing_idle_detector=lambda: None,
        )
        paused, _ = p.should_pause_embedding()
        assert not paused

    def test_typing_policy_disabled(self):
        p = ResourcePolicy(
            pause_on_typing=False,
            battery_detector=lambda: _battery(True),
            typing_idle_detector=lambda: 0.0,    # user is typing right now
        )
        paused, _ = p.should_pause_embedding()
        assert not paused


# ── Concurrent embedding cap ───────────────────────────────────────────

class TestConcurrentEmbeddings:
    def test_explicit_override_wins(self):
        p = ResourcePolicy(
            concurrent_embeddings=2,
            battery_detector=lambda: _battery(True),
            typing_idle_detector=lambda: None,
        )
        assert p.effective_concurrent_embeddings() == 2

    def test_auto_capped_at_default(self):
        p = ResourcePolicy(
            battery_detector=lambda: _battery(True),
            typing_idle_detector=lambda: None,
        )
        # auto = min(DEFAULT_CAP, cpu_count // 2). Whatever it is, it's
        # bounded by the default cap.
        assert p.effective_concurrent_embeddings() <= DEFAULT_CONCURRENT_EMBEDDINGS_CAP
        assert p.effective_concurrent_embeddings() >= 1


# ── Serializable view for the API ─────────────────────────────────────

class TestToDict:
    def test_round_trip_via_to_dict(self):
        p = ResourcePolicy(
            cpu_priority="below_normal",
            pause_on_battery=True,
            pause_on_typing=False,
            typing_idle_seconds=45.0,
            battery_detector=lambda: _battery(True),
            typing_idle_detector=lambda: None,
        )
        d = p.to_dict()
        assert d["cpu_priority"] == "below_normal"
        assert d["pause_on_battery"] is True
        assert d["pause_on_typing"] is False
        assert d["typing_idle_seconds"] == 45.0
