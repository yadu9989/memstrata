"""
Phase 17b — OS keychain secret storage tests.

Uses a mock keyring backed by an in-memory dict so tests run without
touching the real OS keychain. Also verifies the ImportError fallback
paths (keyring not installed).
"""
import sys

import pytest

from memstrata.config.keychain import SERVICE_NAME

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def mock_keyring(monkeypatch):
    """In-memory keyring mock wired into sys.modules."""
    store: dict[tuple[str, str], str] = {}

    class _MockKeyring:
        @staticmethod
        def set_password(service: str, username: str, password: str) -> None:
            store[(service, username)] = password

        @staticmethod
        def get_password(service: str, username: str) -> str | None:
            return store.get((service, username))

        @staticmethod
        def delete_password(service: str, username: str) -> None:
            key = (service, username)
            if key not in store:
                raise Exception("keyring: credential not found")
            del store[key]

    monkeypatch.setitem(sys.modules, "keyring", _MockKeyring())
    return store


@pytest.fixture()
def no_keyring(monkeypatch):
    """Simulate keyring not being installed (sys.modules["keyring"] = None)."""
    monkeypatch.setitem(sys.modules, "keyring", None)


# ── store / get round-trip ────────────────────────────────────────────────────

def test_store_and_get_round_trip(mock_keyring):
    from memstrata.config.keychain import get_api_key, store_api_key
    store_api_key("anthropic", "sk-ant-test-key")
    assert get_api_key("anthropic") == "sk-ant-test-key"


def test_store_and_get_different_providers_are_independent(mock_keyring):
    from memstrata.config.keychain import get_api_key, store_api_key
    store_api_key("anthropic", "sk-ant-abc")
    store_api_key("openai", "sk-openai-xyz")
    assert get_api_key("anthropic") == "sk-ant-abc"
    assert get_api_key("openai") == "sk-openai-xyz"


def test_get_missing_key_returns_none(mock_keyring):
    from memstrata.config.keychain import get_api_key
    assert get_api_key("anthropic") is None


def test_overwrite_updates_stored_value(mock_keyring):
    from memstrata.config.keychain import get_api_key, store_api_key
    store_api_key("anthropic", "old-key")
    store_api_key("anthropic", "new-key")
    assert get_api_key("anthropic") == "new-key"


# ── delete ────────────────────────────────────────────────────────────────────

def test_delete_removes_key(mock_keyring):
    from memstrata.config.keychain import delete_api_key, get_api_key, store_api_key
    store_api_key("anthropic", "sk-ant-test")
    delete_api_key("anthropic")
    assert get_api_key("anthropic") is None


def test_delete_nonexistent_does_not_raise(mock_keyring):
    from memstrata.config.keychain import delete_api_key
    delete_api_key("nonexistent-provider")  # must not raise


def test_delete_leaves_other_providers_intact(mock_keyring):
    from memstrata.config.keychain import delete_api_key, get_api_key, store_api_key
    store_api_key("anthropic", "sk-ant")
    store_api_key("openai", "sk-oai")
    delete_api_key("anthropic")
    assert get_api_key("openai") == "sk-oai"


# ── keyring absent ────────────────────────────────────────────────────────────

def test_store_raises_runtime_error_without_keyring(no_keyring):
    from memstrata.config.keychain import store_api_key
    with pytest.raises(RuntimeError, match="keyring library not available"):
        store_api_key("anthropic", "sk-test")


def test_store_error_message_mentions_pip_install(no_keyring):
    from memstrata.config.keychain import store_api_key
    with pytest.raises(RuntimeError, match="pip install keyring"):
        store_api_key("anthropic", "sk-test")


def test_store_error_message_refuses_plain_files(no_keyring):
    from memstrata.config.keychain import store_api_key
    with pytest.raises(RuntimeError, match="plain files"):
        store_api_key("anthropic", "sk-test")


def test_get_returns_none_without_keyring(no_keyring):
    from memstrata.config.keychain import get_api_key
    assert get_api_key("anthropic") is None


def test_delete_does_not_raise_without_keyring(no_keyring):
    from memstrata.config.keychain import delete_api_key
    delete_api_key("anthropic")  # must not raise


# ── service name ──────────────────────────────────────────────────────────────

def test_service_name_is_memstrata():
    assert SERVICE_NAME == "memstrata"


def test_keys_are_namespaced_by_provider(mock_keyring):
    from memstrata.config.keychain import store_api_key
    store_api_key("anthropic", "key-a")
    store_api_key("openai", "key-b")
    keys = list(mock_keyring.keys())
    services = {k[0] for k in keys}
    assert services == {SERVICE_NAME}, "All keys must share the service name"
    usernames = {k[1] for k in keys}
    assert "api_key:anthropic" in usernames
    assert "api_key:openai" in usernames
