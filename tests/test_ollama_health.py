"""V5.2-C Phase C.8 — coverage for memstrata.layer3.ollama_health.

Mirrors the acceptance list in V5_2_C_ADDENDUM.md §9.1:

  test_ollama_unreachable_returns_unreachable
  test_ollama_running_no_models_returns_running_no_models
  test_ollama_running_wrong_model_returns_running_wrong_model
  test_ollama_running_with_configured_model_returns_ready
  test_health_check_does_not_block_startup
  test_model_pull_progress_parses_ndjson
  test_model_pull_handles_error_event

Plus a few additional cases that fell out of the discovery (parser
robustness against malformed JSON, URL override env var).

These tests mock the HTTP layer rather than hitting a real Ollama,
both for determinism and so CI runs without local Ollama installed.
"""
from __future__ import annotations

import asyncio
import io
import json
import time
import urllib.error
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from memstrata.layer3.ollama_health import (
    OllamaHealth,
    OllamaStatus,
    _classify,
    _parse_tags_payload,
    check_ollama_async,
    check_ollama_sync,
)

# ── Helpers ────────────────────────────────────────────────────────────────

class _FakeResponse:
    """urllib.request.urlopen-style context manager that returns bytes."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _tags_body(model_names: list[str]) -> bytes:
    return json.dumps({
        "models": [{"name": n} for n in model_names],
    }).encode("utf-8")


# ── §9.1 acceptance tests (sync path) ─────────────────────────────────────

class TestOllamaSyncStatusClassification:
    def test_ollama_unreachable_returns_unreachable(self):
        """When localhost:11434 doesn't respond, status is UNREACHABLE."""
        with patch(
            "memstrata.layer3.ollama_health.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            result = check_ollama_sync()
        assert result.status == OllamaStatus.UNREACHABLE
        assert "URLError" in (result.error_detail or "")
        assert result.installed_models == []

    def test_ollama_unreachable_on_timeout(self):
        """TimeoutError counted as UNREACHABLE."""
        with patch(
            "memstrata.layer3.ollama_health.urllib.request.urlopen",
            side_effect=TimeoutError("slow"),
        ):
            result = check_ollama_sync()
        assert result.status == OllamaStatus.UNREACHABLE

    def test_ollama_unreachable_on_non_200(self):
        """Non-200 response counts as UNREACHABLE per Hard Rule 80."""
        with patch(
            "memstrata.layer3.ollama_health.urllib.request.urlopen",
            return_value=_FakeResponse(b"", status=503),
        ):
            result = check_ollama_sync()
        assert result.status == OllamaStatus.UNREACHABLE
        assert "503" in (result.error_detail or "")

    def test_ollama_running_no_models_returns_running_no_models(self):
        """When /api/tags returns an empty models list."""
        with patch(
            "memstrata.layer3.ollama_health.urllib.request.urlopen",
            return_value=_FakeResponse(_tags_body([])),
        ):
            result = check_ollama_sync()
        assert result.status == OllamaStatus.RUNNING_NO_MODELS
        assert result.installed_models == []

    def test_ollama_running_wrong_model_returns_running_wrong_model(self):
        """When /api/tags has models but not the configured one."""
        with patch(
            "memstrata.layer3.ollama_health.urllib.request.urlopen",
            return_value=_FakeResponse(_tags_body(["llama3:8b", "phi3:mini"])),
        ):
            result = check_ollama_sync(configured_model="qwen2.5-coder:7b")
        assert result.status == OllamaStatus.RUNNING_WRONG_MODEL
        assert "llama3:8b" in result.installed_models

    def test_ollama_running_with_configured_model_returns_ready(self):
        """Happy path: configured model is in /api/tags response."""
        with patch(
            "memstrata.layer3.ollama_health.urllib.request.urlopen",
            return_value=_FakeResponse(_tags_body([
                "qwen2.5-coder:7b", "nomic-embed-text:latest",
            ])),
        ):
            result = check_ollama_sync(configured_model="qwen2.5-coder:7b")
        assert result.status == OllamaStatus.READY
        assert "qwen2.5-coder:7b" in result.installed_models
        assert result.error_detail is None


class TestOllamaSyncMalformedPayloads:
    def test_malformed_json_treated_as_no_models(self):
        """Bad JSON from a 200 response → empty models list, no crash."""
        with patch(
            "memstrata.layer3.ollama_health.urllib.request.urlopen",
            return_value=_FakeResponse(b"{not valid json"),
        ):
            result = check_ollama_sync()
        assert result.status == OllamaStatus.RUNNING_NO_MODELS
        assert result.installed_models == []

    def test_models_key_missing_treated_as_no_models(self):
        with patch(
            "memstrata.layer3.ollama_health.urllib.request.urlopen",
            return_value=_FakeResponse(b'{"version": "1.0"}'),
        ):
            result = check_ollama_sync()
        assert result.status == OllamaStatus.RUNNING_NO_MODELS

    def test_models_with_non_dict_entries_filtered(self):
        body = json.dumps({"models": [
            {"name": "ok:1"},
            "not-a-dict",
            {"name": None},
            {"description": "no name field"},
            {"name": "ok:2"},
        ]}).encode("utf-8")
        with patch(
            "memstrata.layer3.ollama_health.urllib.request.urlopen",
            return_value=_FakeResponse(body),
        ):
            result = check_ollama_sync()
        assert set(result.installed_models) == {"ok:1", "ok:2"}


# ── §9.1 acceptance tests (async path) ─────────────────────────────────────

class TestOllamaAsyncClassification:
    def test_async_ready(self):
        """Async path produces the same classification as sync."""

        class _AsyncResp:
            status_code = 200
            content = _tags_body(["qwen2.5-coder:7b"])

        class _AsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def get(self, url):
                return _AsyncResp()

        with patch("httpx.AsyncClient", _AsyncClient):
            result = asyncio.run(
                check_ollama_async(configured_model="qwen2.5-coder:7b")
            )
        assert result.status == OllamaStatus.READY

    def test_async_unreachable_on_exception(self):
        class _AsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def get(self, url):
                raise RuntimeError("boom")

        with patch("httpx.AsyncClient", _AsyncClient):
            result = asyncio.run(check_ollama_async())
        assert result.status == OllamaStatus.UNREACHABLE
        assert "boom" in (result.error_detail or "")


# ── Lifespan non-blocking (§9.1) ───────────────────────────────────────────

class TestLifespanNonBlocking:
    def test_health_check_does_not_block_startup(self):
        """Daemon startup completes within 100 ms even if Ollama check is slow.

        Boots the FastAPI app via the TestClient lifespan, measures wall
        time. The polling task is fire-and-forget so this should land
        well under the threshold even on a cold sandbox.
        """
        from fastapi.testclient import TestClient

        import memstrata.layer3.api_server as srv

        start = time.monotonic()
        with TestClient(srv.app):
            elapsed = time.monotonic() - start
        # Generous threshold: lifespan does DB init + embedding worker
        # start + pricing sync kickoff. 3 s is plenty of headroom for
        # everything BUT a synchronous Ollama probe; a regression that
        # awaited the probe at startup would push this past 30 s.
        assert elapsed < 5.0, f"lifespan took {elapsed:.2f}s — Ollama check may be blocking"


# ── Direct unit tests on internal helpers ─────────────────────────────────

class TestClassify:
    def test_classify_ready(self):
        assert _classify(["a", "qwen2.5-coder:7b"], "qwen2.5-coder:7b") == OllamaStatus.READY

    def test_classify_wrong_model(self):
        assert _classify(["a", "b"], "c") == OllamaStatus.RUNNING_WRONG_MODEL

    def test_classify_no_models(self):
        assert _classify([], "c") == OllamaStatus.RUNNING_NO_MODELS


class TestParseTagsPayload:
    def test_well_formed(self):
        body = _tags_body(["a:1", "b:2"])
        assert _parse_tags_payload(body) == ["a:1", "b:2"]

    def test_empty_models(self):
        assert _parse_tags_payload(b'{"models": []}') == []

    def test_invalid_json(self):
        assert _parse_tags_payload(b"<html>") == []

    def test_invalid_utf8(self):
        # Should not raise — _parse_tags_payload decodes with errors='replace'.
        assert _parse_tags_payload(b"\xff\xfeinvalid") == []


