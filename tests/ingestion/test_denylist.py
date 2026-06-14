"""Tests for V5.2-A Phase 35.5 — denylist + gitignore + binary sniff."""
from __future__ import annotations

from pathlib import Path

import pytest

from memstrata.layer3.ingestion.denylist import (
    DENY_FILE_BASENAMES,
    DENY_FILE_EXTENSIONS,
    HARDCODED_DENYLIST,
    MAX_FILE_SIZE_BYTES,
    SECONDARY_SKIP,
    IndexDecision,
    ProjectSkipPolicy,
    _looks_binary,
    should_index,
    should_walk_dir,
)

# ── Hard Rule 71: hardcoded denylist is comprehensive + frozen ───────────

class TestHardcodedDenylist:
    @pytest.mark.parametrize("name", [
        # Dependency dirs
        "node_modules", ".pnpm", ".yarn", "venv", ".venv",
        "__pycache__", ".pytest_cache", ".mypy_cache",
        "target", "vendor", "Pods",
        "dist", "build", ".next", ".nuxt",
        # VCS
        ".git", ".svn", ".hg",
        # OS / IDE
        ".DS_Store", "Thumbs.db", ".vscode", ".idea",
        # Secrets
        ".ssh", ".aws", ".gnupg", ".config", ".kube", ".docker",
    ])
    def test_each_entry_present(self, name):
        assert name in HARDCODED_DENYLIST

    def test_is_frozen(self):
        # frozenset is immutable; this is the runtime expression of HR 71.
        assert isinstance(HARDCODED_DENYLIST, frozenset)
        with pytest.raises(AttributeError):
            HARDCODED_DENYLIST.add("oops")        # type: ignore[attr-defined]

    def test_file_extensions_frozen(self):
        assert isinstance(DENY_FILE_EXTENSIONS, frozenset)
        with pytest.raises(AttributeError):
            DENY_FILE_EXTENSIONS.add(".oops")     # type: ignore[attr-defined]


# ── should_index / should_walk_dir core behavior ──────────────────────────

class TestShouldIndex:
    def test_indexes_a_normal_python_file(self, tmp_path):
        p = tmp_path / "src" / "app.py"
        p.parent.mkdir(parents=True)
        p.write_text("def hello(): pass\n")
        assert should_index(p, tmp_path).indexed

    def test_skips_inside_node_modules(self, tmp_path):
        p = tmp_path / "node_modules" / "x" / "y.py"
        p.parent.mkdir(parents=True)
        p.write_text("def x(): pass\n")
        d = should_index(p, tmp_path)
        assert not d.indexed
        assert "node_modules" in d.reason

    def test_skips_inside_dot_git(self, tmp_path):
        p = tmp_path / ".git" / "hooks" / "x"
        p.parent.mkdir(parents=True)
        p.write_text("anything")
        assert not should_index(p, tmp_path).indexed

    def test_skips_dot_ds_store_case_insensitive(self, tmp_path):
        p = tmp_path / ".ds_store"
        p.write_text("x")
        d = should_index(p, tmp_path)
        assert not d.indexed

    def test_skips_deny_basename(self, tmp_path):
        for base in DENY_FILE_BASENAMES:
            p = tmp_path / base
            p.write_text("{}")
            d = should_index(p, tmp_path)
            assert not d.indexed, f"{base} should be skipped"

    def test_skips_deny_extension(self, tmp_path):
        p = tmp_path / "weights.safetensors"
        p.write_text("x")
        assert not should_index(p, tmp_path).indexed

    def test_skips_oversize_files(self, tmp_path):
        p = tmp_path / "big.py"
        p.write_bytes(b"x" * (MAX_FILE_SIZE_BYTES + 1024))
        d = should_index(p, tmp_path)
        assert not d.indexed
        assert "too-large" in d.reason

    def test_skips_empty_files(self, tmp_path):
        p = tmp_path / "empty.py"
        p.write_bytes(b"")
        d = should_index(p, tmp_path)
        assert not d.indexed
        assert "empty-file" in d.reason

    def test_outside_project_root_rejected(self, tmp_path):
        outside = tmp_path / "elsewhere.py"
        outside.write_text("x = 1\n")
        bogus_root = tmp_path / "subdir"
        bogus_root.mkdir()
        d = should_index(outside, bogus_root)
        assert not d.indexed


# ── SECONDARY_SKIP + user overrides ──────────────────────────────────────

class TestSecondarySkip:
    def test_default_skips_data_dir(self, tmp_path):
        p = tmp_path / "data" / "rows.py"
        p.parent.mkdir(parents=True)
        p.write_text("x = 1\n")
        d = should_index(p, tmp_path)
        assert not d.indexed
        assert "secondary-skip" in d.reason

    def test_user_added_dirs_overrides_secondary_skip(self, tmp_path):
        p = tmp_path / "data" / "rows.py"
        p.parent.mkdir(parents=True)
        p.write_text("x = 1\n")
        policy = ProjectSkipPolicy(user_added_dirs=frozenset({"data"}))
        d = should_index(p, tmp_path, policy=policy)
        assert d.indexed

    def test_user_excluded_dirs_skips(self, tmp_path):
        p = tmp_path / "src" / "secrets.py"
        p.parent.mkdir(parents=True)
        p.write_text("x = 1\n")
        policy = ProjectSkipPolicy(user_excluded_dirs=frozenset({"src"}))
        d = should_index(p, tmp_path, policy=policy)
        assert not d.indexed
        assert "user-excluded" in d.reason

    def test_user_added_dirs_cannot_override_hard_denylist(self, tmp_path):
        """Hard Rule 71: even user-added-dirs can't reach into node_modules."""
        p = tmp_path / "node_modules" / "x.py"
        p.parent.mkdir(parents=True)
        p.write_text("x = 1\n")
        policy = ProjectSkipPolicy(user_added_dirs=frozenset({"node_modules"}))
        d = should_index(p, tmp_path, policy=policy)
        assert not d.indexed
        assert "denylisted-dir" in d.reason


# ── Binary content sniff §4.2 ────────────────────────────────────────────

class TestBinarySniff:
    def test_text_file_passes(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("def hello(): pass\n")
        assert not _looks_binary(p)

    def test_file_with_nul_byte_flagged(self, tmp_path):
        p = tmp_path / "weird.py"
        p.write_bytes(b"hello\x00world\n")
        assert _looks_binary(p)

    def test_should_index_rejects_binary(self, tmp_path):
        p = tmp_path / "fake.py"
        p.write_bytes(b"\x00\x00\x00\x00\xff\xff\xff\xff" * 100)
        d = should_index(p, tmp_path)
        assert not d.indexed
        assert d.reason == "binary-content"


# ── Directory walk gate ──────────────────────────────────────────────────

class TestShouldWalkDir:
    def test_walks_normal_dir(self, tmp_path):
        d = tmp_path / "src"
        d.mkdir()
        assert should_walk_dir(d, tmp_path).indexed

    def test_does_not_walk_node_modules(self, tmp_path):
        d = tmp_path / "node_modules"
        d.mkdir()
        assert not should_walk_dir(d, tmp_path).indexed
