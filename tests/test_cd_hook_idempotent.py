"""
Phase 15' — cd-hook idempotency and file I/O tests.

Verifies write_hook / remove_hook behavior: backup creation, idempotent
replacement, no duplication, and clean removal.
"""
import pytest
from pathlib import Path

from memory_layer.cli.cd_hook import (
    write_hook,
    remove_hook,
    _HOOK_MARKER_BEGIN,
    _HOOK_MARKER_END,
)

EXISTING_CONTENT = "# existing shell config\nexport FOO=bar\n"


# ── write_hook ────────────────────────────────────────────────────────────────

def test_write_hook_appends_to_existing_file(tmp_path):
    config = tmp_path / ".zshrc"
    config.write_text(EXISTING_CONTENT, encoding="utf-8")
    write_hook("zsh", config)
    result = config.read_text(encoding="utf-8")
    assert "export FOO=bar" in result
    assert _HOOK_MARKER_BEGIN in result
    assert "ml_cd_hook" in result


def test_write_hook_creates_file_when_absent(tmp_path):
    config = tmp_path / ".zshrc"
    write_hook("zsh", config)
    assert config.exists()
    assert _HOOK_MARKER_BEGIN in config.read_text(encoding="utf-8")


def test_write_hook_creates_backup_on_first_write(tmp_path):
    config = tmp_path / ".zshrc"
    config.write_text(EXISTING_CONTENT, encoding="utf-8")
    write_hook("zsh", config)
    backup = tmp_path / ".zshrc.ml-backup"
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == EXISTING_CONTENT


def test_backup_not_overwritten_on_second_write(tmp_path):
    """Backup is created once from the original; repeated writes don't touch it."""
    config = tmp_path / ".zshrc"
    config.write_text(EXISTING_CONTENT, encoding="utf-8")
    write_hook("zsh", config)
    write_hook("zsh", config)
    backup = tmp_path / ".zshrc.ml-backup"
    assert backup.read_text(encoding="utf-8") == EXISTING_CONTENT


def test_write_hook_does_not_duplicate_markers(tmp_path):
    config = tmp_path / ".zshrc"
    config.write_text(EXISTING_CONTENT, encoding="utf-8")
    write_hook("zsh", config)
    write_hook("zsh", config)
    content = config.read_text(encoding="utf-8")
    assert content.count(_HOOK_MARKER_BEGIN) == 1
    assert content.count(_HOOK_MARKER_END) == 1


def test_write_hook_twice_yields_same_content(tmp_path):
    """Writing once and twice should produce the same file."""
    config = tmp_path / ".zshrc"
    config.write_text(EXISTING_CONTENT, encoding="utf-8")
    write_hook("zsh", config)
    after_first = config.read_text(encoding="utf-8")
    write_hook("zsh", config)
    after_second = config.read_text(encoding="utf-8")
    assert after_first == after_second


def test_write_hook_preserves_content_after_hook_block(tmp_path):
    """Content that comes AFTER the hook block must be retained on re-write."""
    config = tmp_path / ".zshrc"
    config.write_text("# pre\n", encoding="utf-8")
    write_hook("zsh", config)
    # Simulate the user adding content after the hook
    with open(config, "a", encoding="utf-8") as f:
        f.write("\n# post-hook content\n")
    write_hook("zsh", config)
    result = config.read_text(encoding="utf-8")
    assert "# post-hook content" in result
    assert result.count(_HOOK_MARKER_BEGIN) == 1


def test_write_hook_creates_parent_directories(tmp_path):
    """Fish config lives in a nested directory; write_hook must create it."""
    config = tmp_path / ".config" / "fish" / "config.fish"
    write_hook("fish", config)
    assert config.exists()


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "powershell"])
def test_all_shells_idempotent(tmp_path, shell):
    config = tmp_path / f"config_{shell}"
    config.write_text("# pre-existing\n", encoding="utf-8")
    write_hook(shell, config)
    write_hook(shell, config)
    assert config.read_text(encoding="utf-8").count(_HOOK_MARKER_BEGIN) == 1


# ── remove_hook ───────────────────────────────────────────────────────────────

def test_remove_hook_strips_block(tmp_path):
    config = tmp_path / ".zshrc"
    config.write_text(EXISTING_CONTENT, encoding="utf-8")
    write_hook("zsh", config)
    remove_hook(config)
    cleaned = config.read_text(encoding="utf-8")
    assert _HOOK_MARKER_BEGIN not in cleaned
    assert _HOOK_MARKER_END not in cleaned


def test_remove_hook_preserves_surrounding_content(tmp_path):
    config = tmp_path / ".zshrc"
    config.write_text(EXISTING_CONTENT, encoding="utf-8")
    write_hook("zsh", config)
    remove_hook(config)
    cleaned = config.read_text(encoding="utf-8")
    assert "export FOO=bar" in cleaned


def test_remove_hook_no_op_when_hook_absent(tmp_path):
    config = tmp_path / ".zshrc"
    config.write_text(EXISTING_CONTENT, encoding="utf-8")
    remove_hook(config)
    assert config.read_text(encoding="utf-8") == EXISTING_CONTENT


def test_remove_hook_no_op_on_missing_file(tmp_path):
    config = tmp_path / ".nonexistent_config"
    remove_hook(config)  # must not raise


def test_write_then_remove_then_write_is_idempotent(tmp_path):
    """Write → Remove → Write produces same result as a single Write."""
    config = tmp_path / ".zshrc"
    config.write_text(EXISTING_CONTENT, encoding="utf-8")

    write_hook("zsh", config)
    after_first_write = config.read_text(encoding="utf-8")

    remove_hook(config)
    write_hook("zsh", config)
    after_reinstall = config.read_text(encoding="utf-8")

    assert after_first_write == after_reinstall
