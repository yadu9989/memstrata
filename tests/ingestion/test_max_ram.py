"""Tests for V5.2-A Phase 35.6 — max-RAM enforcement in ResourcePolicy."""
from __future__ import annotations

import pytest

from memstrata.layer3.ingestion.resource_policy import (
    BatteryState,
    ResourcePolicy,
)


def _ac_battery() -> BatteryState:
    return BatteryState(on_battery_power=False)


def _no_idle() -> None:
    return None


class TestMaxRAM:
    def test_does_not_pause_when_rss_under_cap(self):
        p = ResourcePolicy(
            max_ram_bytes=1 * 1024 * 1024 * 1024,         # 1 GB cap
            memory_probe=lambda: 500 * 1024 * 1024,       # 500 MB
            battery_detector=_ac_battery,
            typing_idle_detector=_no_idle,
        )
        paused, reason = p.should_pause_embedding()
        assert not paused
        assert reason == ""

    def test_pauses_when_rss_over_cap(self):
        p = ResourcePolicy(
            max_ram_bytes=512 * 1024 * 1024,              # 512 MB
            memory_probe=lambda: 700 * 1024 * 1024,       # 700 MB
            battery_detector=_ac_battery,
            typing_idle_detector=_no_idle,
        )
        paused, reason = p.should_pause_embedding()
        assert paused
        assert reason.startswith("max-ram:")
        assert "700MB" in reason
        assert "512MB" in reason

    def test_undetectable_rss_does_not_pause(self):
        """None from the probe -> can't tell -> don't pause."""
        p = ResourcePolicy(
            max_ram_bytes=64 * 1024 * 1024,
            memory_probe=lambda: None,
            battery_detector=_ac_battery,
            typing_idle_detector=_no_idle,
        )
        paused, _ = p.should_pause_embedding()
        assert not paused

    def test_max_ram_zero_disables_check(self):
        """max_ram_bytes <= 0 -> RAM enforcement off."""
        p = ResourcePolicy(
            max_ram_bytes=0,
            memory_probe=lambda: 999 * 1024 * 1024 * 1024,   # 999 GB
            battery_detector=_ac_battery,
            typing_idle_detector=_no_idle,
        )
        paused, _ = p.should_pause_embedding()
        assert not paused

    def test_ram_check_evaluated_before_battery(self):
        """Order matters: RAM is the only check that can hurt the host
        (OOM kill / swap thrash). It should fire before battery so the
        reason field reflects the actual hazard."""
        p = ResourcePolicy(
            max_ram_bytes=100 * 1024 * 1024,             # 100 MB
            memory_probe=lambda: 200 * 1024 * 1024,      # 200 MB
            battery_detector=lambda: BatteryState(on_battery_power=True),
            typing_idle_detector=_no_idle,
        )
        paused, reason = p.should_pause_embedding()
        assert paused
        assert reason.startswith("max-ram:"), f"got {reason!r}"
