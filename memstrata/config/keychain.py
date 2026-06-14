"""
OS keychain secret storage for API keys.

Uses the `keyring` Python library which abstracts over:
  - macOS Keychain
  - Windows Credential Manager
  - Linux Secret Service (libsecret) / kwallet

Fails with RuntimeError if keyring is not installed — never writes
API keys to disk. Pattern taken verbatim from
v5_1_reference/critical_snippets.py §4.
"""
from __future__ import annotations

from typing import Optional

SERVICE_NAME = "memstrata"


def store_api_key(provider: str, api_key: str) -> None:
    """Store an API key in the OS keychain. Never persisted to disk."""
    try:
        import keyring
    except ImportError as e:
        raise RuntimeError(
            "keyring library not available. Install via `pip install keyring`. "
            "Refusing to write API keys to plain files."
        ) from e
    keyring.set_password(SERVICE_NAME, f"api_key:{provider}", api_key)


def get_api_key(provider: str) -> str | None:
    """Retrieve a stored API key. Returns None if keyring is absent or key not found."""
    try:
        import keyring
        return keyring.get_password(SERVICE_NAME, f"api_key:{provider}")
    except ImportError:
        return None


def delete_api_key(provider: str) -> None:
    """Remove an API key from the keychain. No-op if not present."""
    try:
        import keyring
        keyring.delete_password(SERVICE_NAME, f"api_key:{provider}")
    except (ImportError, Exception):
        pass
