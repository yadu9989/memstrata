"""Resource policy — V5.2-A Phase 35.4.

The orchestrator consults a ``ResourcePolicy`` instance between batches
to decide whether to keep going or pause. Default policy matches the
V5_2_A_ADDENDUM §3.5 table:

  CPU priority             BELOW_NORMAL (Win) / nice +10 (Unix)
  Concurrent embeddings    4 or cpu_count // 2, whichever is lower
  Pause when on battery    Yes
  Pause when active typing Yes  (last input within 30s)
  Max RAM                  1 GB

"Active typing" pauses EMBED only — parse work is cheap and continues so
the user gets immediate retrieval feedback the moment they go idle.

Detection backends fall back to "policy off" when the platform can't
expose the signal; we'd rather over-index slightly than block on a
machine we can't probe.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Optional

_LOG = logging.getLogger(__name__)

DEFAULT_TYPING_IDLE_SECONDS = 30.0
DEFAULT_MAX_RAM_BYTES = 1 * 1024 * 1024 * 1024     # 1 GB
DEFAULT_CONCURRENT_EMBEDDINGS_CAP = 4


@dataclass
class ResourcePolicy:
    """Composable policy. Each field can be overridden by the wizard."""

    cpu_priority: str = "below_normal"             # 'below_normal' | 'normal'
    concurrent_embeddings: int | None = None    # None = auto
    pause_on_battery: bool = True
    pause_on_typing: bool = True
    typing_idle_seconds: float = DEFAULT_TYPING_IDLE_SECONDS
    max_ram_bytes: int = DEFAULT_MAX_RAM_BYTES

    # Injectable for tests so we don't have to mock OS APIs everywhere.
    battery_detector: Callable[[], BatteryState] = field(
        default_factory=lambda: detect_battery_state
    )
    typing_idle_detector: Callable[[], float | None] = field(
        default_factory=lambda: detect_idle_seconds
    )
    # V5.2-A Phase 35.6 — RSS probe for the max-RAM check. Returns None
    # when psutil isn't available; the policy treats None as "can't tell
    # -> don't pause" so a stripped-down build still indexes.
    memory_probe: Callable[[], int | None] = field(
        default_factory=lambda: current_rss_bytes
    )

    # ── Concurrent embedding cap ───────────────────────────────────────

    def effective_concurrent_embeddings(self) -> int:
        if self.concurrent_embeddings is not None and self.concurrent_embeddings > 0:
            return self.concurrent_embeddings
        cpu = max(1, os.cpu_count() or 1)
        return max(1, min(DEFAULT_CONCURRENT_EMBEDDINGS_CAP, cpu // 2 or 1))

    # ── Pause checks ──────────────────────────────────────────────────

    def should_pause_embedding(self) -> tuple[bool, str]:
        """Return ``(pause, reason)``.

        Reasons are human-readable; the orchestrator surfaces them to
        the progress UI so the user knows why indexing is idle.

        Order matters — we pause on the FIRST trigger found and don't
        evaluate the rest. The max-RAM check goes first because it's
        the only condition that can actively harm the host (OOM kill,
        swap thrashing) rather than merely degrade UX.
        """
        # V5.2-A Phase 35.6 — RAM enforcement.
        if self.max_ram_bytes > 0:
            rss = self.memory_probe()
            if rss is not None and rss > self.max_ram_bytes:
                return (
                    True,
                    f"max-ram:{rss // (1024 * 1024)}MB>"
                    f"{self.max_ram_bytes // (1024 * 1024)}MB",
                )

        if self.pause_on_battery:
            battery = self.battery_detector()
            if battery.on_battery_power:
                return True, "on-battery"

        if self.pause_on_typing:
            idle = self.typing_idle_detector()
            # idle is None when the platform can't tell. We don't pause
            # in that case (the alternative is "never index on this OS").
            if idle is not None and idle < self.typing_idle_seconds:
                return True, f"recent-input:{idle:.1f}s"

        return False, ""

    def to_dict(self) -> dict:
        """Serializable view for the progress endpoint."""
        return {
            "cpu_priority": self.cpu_priority,
            "concurrent_embeddings": self.effective_concurrent_embeddings(),
            "pause_on_battery": self.pause_on_battery,
            "pause_on_typing": self.pause_on_typing,
            "typing_idle_seconds": self.typing_idle_seconds,
            "max_ram_bytes": self.max_ram_bytes,
        }


# ── Battery detection ────────────────────────────────────────────────────

@dataclass(frozen=True)
class BatteryState:
    on_battery_power: bool       # True when running on battery
    percent: float | None = None
    detectable: bool = True      # False -> we couldn't read the signal


def detect_battery_state() -> BatteryState:
    """Cross-platform battery probe. Returns ``BatteryState(False, ...)``
    on desktops without a battery — the typical "always plugged in"
    machine should never pause for battery reasons.
    """
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return BatteryState(False, None, detectable=False)
    try:
        battery = psutil.sensors_battery()
    except Exception as exc:                       # noqa: BLE001
        _LOG.debug("psutil.sensors_battery raised: %s", exc)
        return BatteryState(False, None, detectable=False)
    if battery is None:
        # Desktop / VM with no battery sensor — definitely on AC.
        return BatteryState(False, None, detectable=True)
    return BatteryState(
        on_battery_power=not battery.power_plugged,
        percent=float(battery.percent) if battery.percent is not None else None,
    )


# ── Typing / input idle detection ────────────────────────────────────────

def detect_idle_seconds() -> float | None:
    """Seconds since the user's last keyboard / mouse event, or None.

    Implementations:
      * Windows : ctypes GetLastInputInfo + GetTickCount.
      * macOS   : `ioreg -c IOHIDSystem` -> HIDIdleTime (nanoseconds).
      * Linux   : `xprintidle` if installed; None otherwise (Wayland has
                  no standard idle-time API outside per-DE protocols).

    None means "can't tell" — the policy treats it as "user not idle and
    not active", which under default settings does NOT pause indexing.
    """
    if sys.platform.startswith("win"):
        return _windows_idle_seconds()
    if sys.platform == "darwin":
        return _macos_idle_seconds()
    if sys.platform.startswith("linux"):
        return _linux_idle_seconds()
    return None


def _windows_idle_seconds() -> float | None:
    try:
        import ctypes
        from ctypes import wintypes  # type: ignore[attr-defined]

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):  # type: ignore[attr-defined]
            return None
        # GetTickCount wraps every ~49.7 days — close enough for idle math.
        now = ctypes.windll.kernel32.GetTickCount()                        # type: ignore[attr-defined]
        return max(0.0, (now - lii.dwTime) / 1000.0)
    except Exception as exc:                       # noqa: BLE001
        _LOG.debug("Windows idle probe failed: %s", exc)
        return None


def _macos_idle_seconds() -> float | None:
    """Parse `ioreg -c IOHIDSystem` for the HIDIdleTime field (ns)."""
    try:
        result = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except Exception as exc:                       # noqa: BLE001
        _LOG.debug("ioreg invocation failed: %s", exc)
        return None
    if result.returncode != 0:
        return None
    import re
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', result.stdout)
    if not m:
        return None
    return int(m.group(1)) / 1_000_000_000.0


def _linux_idle_seconds() -> float | None:
    """Best-effort: `xprintidle` returns ms idle on X11.

    Wayland has no standardized cross-desktop idle API; we return None
    there so the policy doesn't pause indefinitely. Operator can
    explicitly disable typing-pause via the wizard if they prefer.
    """
    exe = shutil.which("xprintidle")
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe], capture_output=True, text=True, timeout=2.0, check=False,
        )
    except Exception as exc:                       # noqa: BLE001
        _LOG.debug("xprintidle failed: %s", exc)
        return None
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip()) / 1000.0
    except ValueError:
        return None


# ── CPU priority application ────────────────────────────────────────────

def apply_cpu_priority(level: str = "below_normal") -> bool:
    """Set the calling process's scheduling priority.

    Returns True on success. Failures are logged + ignored — the policy
    must NOT crash the orchestrator on a sandboxed machine that won't
    let us renice.
    """
    if level not in ("below_normal", "normal"):
        raise ValueError(f"unknown priority level: {level}")
    if level == "normal":
        return True       # nothing to do; we never raise priority
    if sys.platform.startswith("win"):
        return _windows_set_below_normal()
    return _unix_renice()


def _windows_set_below_normal() -> bool:
    try:
        import ctypes
        PROCESS_SET_INFORMATION = 0x0200
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        handle = ctypes.windll.kernel32.GetCurrentProcess()         # type: ignore[attr-defined]
        ok = ctypes.windll.kernel32.SetPriorityClass(               # type: ignore[attr-defined]
            handle, BELOW_NORMAL_PRIORITY_CLASS,
        )
        return bool(ok)
    except Exception as exc:                       # noqa: BLE001
        _LOG.debug("SetPriorityClass failed: %s", exc)
        return False


def _unix_renice() -> bool:
    try:
        # nice +10 is the spec's target; we use os.nice which renices
        # the calling thread + all descendants.
        os.nice(10)
        return True
    except OSError as exc:
        _LOG.debug("os.nice(10) failed: %s", exc)
        return False
    except AttributeError:
        # os.nice doesn't exist on stripped-down Python builds.
        return False


# ── RAM check ──────────────────────────────────────────────────────────

def current_rss_bytes() -> int | None:
    """Resident set size for this process, or None if not probeable."""
    try:
        import psutil  # type: ignore[import-not-found]
        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:                              # noqa: BLE001
        return None
