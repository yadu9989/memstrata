"""Ollama reachability + model presence — V5.2-C Phase C.1.

One shared module so both the synchronous `doctor` CLI and the async
MIT-Core background polling loop call the same logic. Don't duplicate
the wire shape — Ollama's ``/api/tags`` response format and the
classification rules belong in one place.

Surfaces:

  check_ollama_sync(timeout=2.0)   — stdlib-only, used by doctor.py
  check_ollama_async(timeout=2.0)  — httpx-based, used by api_server lifespan

Both return an ``OllamaHealth`` dataclass with the classified status,
the list of installed model names, the configured model the daemon is
looking for, and a free-form error_detail for diagnostics.

Hard Rule 80 posture: every exception inside this module is caught and
mapped to ``OllamaStatus.UNREACHABLE``. Callers can rely on these
functions never raising.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

_LOG = logging.getLogger(__name__)


# ── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_OLLAMA_URL = os.environ.get("ML_OLLAMA_URL", "http://localhost:11434")
DEFAULT_LOCAL_LLM_MODEL = os.environ.get("ML_LOCAL_LLM_MODEL", "qwen2.5-coder:7b")
DEFAULT_TIMEOUT_SECONDS = 2.0


# ── Status enum ────────────────────────────────────────────────────────────

class OllamaStatus(str, Enum):
    """Per V5.2-C §3.1. str-subclass so FastAPI serializes the value directly."""
    READY = "ready"
    RUNNING_NO_MODELS = "running_no_models"
    RUNNING_WRONG_MODEL = "running_wrong_model"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


@dataclass
class OllamaHealth:
    """Snapshot returned by check_ollama_sync / check_ollama_async."""
    status: OllamaStatus
    configured_model: str
    installed_models: list[str] = field(default_factory=list)
    error_detail: str | None = None


# ── Internal classification ────────────────────────────────────────────────

def _classify(installed_models: list[str], configured_model: str) -> OllamaStatus:
    if configured_model in installed_models:
        return OllamaStatus.READY
    if installed_models:
        return OllamaStatus.RUNNING_WRONG_MODEL
    return OllamaStatus.RUNNING_NO_MODELS


def _parse_tags_payload(raw: bytes) -> list[str]:
    """Pull the model name list out of /api/tags. Empty list on any parse error."""
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return []
    models = data.get("models", []) if isinstance(data, dict) else []
    if not isinstance(models, list):
        return []
    out: list[str] = []
    for entry in models:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name:
                out.append(name)
    return out


# ── Sync path (doctor.py) ──────────────────────────────────────────────────

def check_ollama_sync(
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    configured_model: str = DEFAULT_LOCAL_LLM_MODEL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> OllamaHealth:
    """Stdlib-only Ollama reachability + model presence check.

    Used by the `doctor` CLI so it stays dependency-free (no httpx
    import on the diagnostic path). Catches every exception — Hard
    Rule 80: this function never raises.
    """
    url = f"{ollama_url.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:    # noqa: S310 — localhost URL
            if resp.status != 200:
                return OllamaHealth(
                    status=OllamaStatus.UNREACHABLE,
                    configured_model=configured_model,
                    error_detail=f"HTTP {resp.status} from {url}",
                )
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return OllamaHealth(
            status=OllamaStatus.UNREACHABLE,
            configured_model=configured_model,
            error_detail=f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:                                          # noqa: BLE001 — Hard Rule 80
        return OllamaHealth(
            status=OllamaStatus.UNREACHABLE,
            configured_model=configured_model,
            error_detail=f"unexpected {type(exc).__name__}: {exc}",
        )

    installed = _parse_tags_payload(raw)
    return OllamaHealth(
        status=_classify(installed, configured_model),
        configured_model=configured_model,
        installed_models=installed,
    )


# ── Async path (api_server.py lifespan) ────────────────────────────────────

async def check_ollama_async(
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    configured_model: str = DEFAULT_LOCAL_LLM_MODEL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> OllamaHealth:
    """Async Ollama reachability + model presence check.

    Used by the MIT-Core background polling task. httpx is already a
    runtime dep of memstrata (see pricing.openrouter_sync). Catches
    every exception — Hard Rule 80: this function never raises.
    """
    try:
        import httpx
    except ImportError as exc:
        return OllamaHealth(
            status=OllamaStatus.UNREACHABLE,
            configured_model=configured_model,
            error_detail=f"httpx import failed: {exc}",
        )

    url = f"{ollama_url.rstrip('/')}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            if response.status_code != 200:
                return OllamaHealth(
                    status=OllamaStatus.UNREACHABLE,
                    configured_model=configured_model,
                    error_detail=f"HTTP {response.status_code} from {url}",
                )
            raw = response.content
    except Exception as exc:                                          # noqa: BLE001 — Hard Rule 80
        return OllamaHealth(
            status=OllamaStatus.UNREACHABLE,
            configured_model=configured_model,
            error_detail=f"{type(exc).__name__}: {exc}",
        )

    installed = _parse_tags_payload(raw)
    return OllamaHealth(
        status=_classify(installed, configured_model),
        configured_model=configured_model,
        installed_models=installed,
    )
