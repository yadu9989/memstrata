"""Indexing denylist + gitignore integration — V5.2-A Phase 35.5.

Three layers (precedence highest-first):

  1. HARDCODED_DENYLIST          — Hard Rule 71. Not user-overridable.
                                   Skips dependency dirs, VCS internals,
                                   secret stores, build outputs.
  2. .gitignore                   — Respected per Hard Rule 71. We never
                                   index files git itself ignores.
  3. SECONDARY_SKIP (user-tunable) — Per-project skip list the wizard
                                   surfaces in §6.2.  Each entry can be
                                   un-checked by the user.

A file is indexed iff:

  * Its parent directories don't intersect HARDCODED_DENYLIST.
  * Its parent directories don't intersect SECONDARY_SKIP UNLESS the user
    added them to project_opt_in.user_added_dirs.
  * Its extension isn't in DENY_FILE_EXTENSIONS.
  * Its size is <= MAX_FILE_SIZE_BYTES.
  * It looks like text (binary-content sniff per §4.2).
  * .gitignore doesn't ignore it (when pathspec is available).
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Optional

_LOG = logging.getLogger(__name__)

# ── §4: HARDCODED_DENYLIST (Hard Rule 71 — NOT user-overridable) ────────────

HARDCODED_DENYLIST: frozenset[str] = frozenset({
    # Dependency directories
    "node_modules", ".pnpm", ".yarn", "bower_components",
    "venv", ".venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "site-packages",
    "target",                              # Rust
    "vendor",                              # Go, PHP
    ".bundle",                             # Ruby
    "Pods",                                # iOS
    ".gradle", "build", ".build",          # JVM / general build outputs
    "dist", "out", ".next", ".nuxt",
    ".svelte-kit", ".astro",               # Web build

    # Version control internals
    ".git", ".svn", ".hg",

    # OS / IDE internals
    ".DS_Store", "Thumbs.db", ".vscode", ".idea", ".vs",

    # Secrets and sensitive directories
    ".ssh", ".aws", ".gcp", ".config", ".gnupg",
    ".kube", ".docker",

    # Logs and runtime artifacts
    "logs", "tmp", "temp",

    # Large binary directories common in ML
    "checkpoints", "wandb", "mlruns", ".dvc",
})

DENY_FILE_EXTENSIONS: frozenset[str] = frozenset({
    # Binary
    ".exe", ".dll", ".so", ".dylib", ".bin", ".obj", ".o", ".a",
    ".jar", ".war", ".ear", ".class",
    ".pyc", ".pyo", ".pyd",

    # Media
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".mp3", ".wav", ".ogg", ".flac",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".tiff",
    ".pdf", ".psd", ".ai", ".sketch",

    # Archives
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",

    # Databases
    ".db", ".sqlite", ".sqlite3", ".duckdb",

    # Model weights
    ".pt", ".pth", ".ckpt", ".safetensors", ".gguf", ".onnx",
})

# Filename basenames the spec ignores (lock files). Distinct from
# DENY_FILE_EXTENSIONS because their extension (.json, .yaml, etc.) is
# generally fine.
DENY_FILE_BASENAMES: frozenset[str] = frozenset({
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Pipfile.lock",
    "poetry.lock",
    "composer.lock",
    "Cargo.lock",
    "Gemfile.lock",
})

# Files larger than this are skipped regardless of extension.
MAX_FILE_SIZE_BYTES: int = 1_000_000        # 1MB

# ── §4.1: SECONDARY_SKIP (user-overridable per project) ─────────────────────

SECONDARY_SKIP: frozenset[str] = frozenset({
    "data", "datasets",
    "fixtures", "mocks",
    "docs/build", "site",
    "coverage", ".coverage",
    "storybook-static",
})

# ── Public API ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IndexDecision:
    """Why a path was or wasn't indexed. Useful for debug surfaces."""
    indexed: bool
    reason: str

    @classmethod
    def yes(cls, reason: str = "") -> IndexDecision:
        return cls(True, reason)

    @classmethod
    def no(cls, reason: str) -> IndexDecision:
        return cls(False, reason)


@dataclass(frozen=True)
class ProjectSkipPolicy:
    """Per-project user choices over SECONDARY_SKIP.

    ``user_added_dirs``: SECONDARY_SKIP entries the user un-skipped (e.g.,
    they DO want ``data/`` indexed for this project).

    ``user_excluded_dirs``: extra paths the user wants skipped beyond
    HARDCODED_DENYLIST + SECONDARY_SKIP.
    """
    user_added_dirs: frozenset[str] = field(default_factory=frozenset)
    user_excluded_dirs: frozenset[str] = field(default_factory=frozenset)


def _path_parts(rel: PurePath) -> tuple[str, ...]:
    """Tuple of path parts safe for cross-OS denylist matching."""
    return rel.parts


def _decide(
    full_path: Path,
    project_root: Path,
    *,
    policy: ProjectSkipPolicy = ProjectSkipPolicy(),
) -> IndexDecision:
    """Core decision used by ``should_index`` and ``should_walk_dir``."""
    try:
        rel = full_path.resolve().relative_to(project_root.resolve())
    except (ValueError, OSError):
        return IndexDecision.no("outside-project-root")

    parts = _path_parts(rel)
    parts_lower = tuple(p.lower() for p in parts)

    # 1. HARDCODED_DENYLIST — not user-overridable (Hard Rule 71).
    for part in parts:
        if part in HARDCODED_DENYLIST:
            return IndexDecision.no(f"denylisted-dir:{part}")
        # Match case-insensitive for the OS-flavored entries (.DS_Store etc).
        if part.lower() in {d.lower() for d in HARDCODED_DENYLIST}:
            return IndexDecision.no(f"denylisted-dir:{part}")

    # 2. SECONDARY_SKIP minus user opt-ins, plus extra user excludes.
    for part in parts:
        if part in policy.user_excluded_dirs:
            return IndexDecision.no(f"user-excluded:{part}")
        if part in SECONDARY_SKIP and part not in policy.user_added_dirs:
            return IndexDecision.no(f"secondary-skip:{part}")
    return IndexDecision.yes()


def should_walk_dir(
    dir_path: Path,
    project_root: Path,
    *,
    policy: ProjectSkipPolicy = ProjectSkipPolicy(),
) -> IndexDecision:
    """Whether a directory's contents should be recursed into."""
    return _decide(dir_path, project_root, policy=policy)


def should_index(
    file_path: Path,
    project_root: Path,
    *,
    policy: ProjectSkipPolicy = ProjectSkipPolicy(),
    gitignore_matcher: object | None = None,
) -> IndexDecision:
    """Whether a single file should be passed to the chunker.

    ``gitignore_matcher`` is whatever ``load_gitignore`` returns — opaque to
    callers; passed through to the matching code below.
    """
    # Directory-level checks first (catches files under denylisted dirs
    # even when callers reach them via direct glob rather than walk).
    decision = _decide(file_path, project_root, policy=policy)
    if not decision.indexed:
        return decision

    # 3. Filename / extension.
    name = file_path.name
    if name in DENY_FILE_BASENAMES:
        return IndexDecision.no(f"deny-basename:{name}")
    if file_path.suffix.lower() in DENY_FILE_EXTENSIONS:
        return IndexDecision.no(f"deny-extension:{file_path.suffix.lower()}")

    # 4. Size cap.
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        return IndexDecision.no(f"stat-failed:{exc}")
    if size > MAX_FILE_SIZE_BYTES:
        return IndexDecision.no(f"too-large:{size}")
    if size == 0:
        return IndexDecision.no("empty-file")

    # 5. .gitignore (when a matcher was loaded).
    if gitignore_matcher is not None:
        try:
            rel = str(file_path.resolve().relative_to(project_root.resolve()))
            if _gitignore_matches(gitignore_matcher, rel):
                return IndexDecision.no("gitignore")
        except Exception as exc:                  # noqa: BLE001 — never fail-closed
            _LOG.debug("gitignore match failed for %s: %s", file_path, exc)

    # 6. Binary content sniff per §4.2.
    if _looks_binary(file_path):
        return IndexDecision.no("binary-content")

    return IndexDecision.yes()


# ── §4.2: binary content sniff ─────────────────────────────────────────────

# Read this many bytes from the head of each file to decide text-vs-binary.
_BINARY_SNIFF_BYTES = 8 * 1024


def _looks_binary(path: Path) -> bool:
    """True when the first ``_BINARY_SNIFF_BYTES`` look like binary.

    Heuristic: a file is binary if it contains NUL bytes in its first
    8KB. This is the same rule git uses to decide "binary" for diff
    rendering. It's noisy on UTF-16 / UTF-32 files (they contain NULs
    by design) but those are vanishingly rare in code repos.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return True   # can't read -> can't index, treat as binary
    return b"\x00" in head


# ── .gitignore loading (best-effort; pathspec optional) ────────────────────

def load_gitignore(project_root: Path):
    """Return an opaque matcher for the project's combined .gitignore rules.

    Reads every ``.gitignore`` file in the tree (root + nested), feeds them
    into ``pathspec`` if it's installed. Returns ``None`` when no rules
    found or when pathspec is unavailable — callers should treat None as
    "no gitignore matching".
    """
    try:
        import pathspec  # type: ignore[import-not-found]
    except ImportError:
        return None
    patterns: list[str] = []
    try:
        for gi in project_root.rglob(".gitignore"):
            if any(p in HARDCODED_DENYLIST for p in gi.relative_to(project_root).parts):
                continue
            try:
                patterns.extend(
                    line for line in gi.read_text(encoding="utf-8", errors="replace").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                )
            except OSError:
                continue
    except OSError as exc:
        _LOG.debug("gitignore walk failed for %s: %s", project_root, exc)
        return None
    if not patterns:
        return None
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def _gitignore_matches(matcher, relative_path: str) -> bool:
    """Apply the matcher; True when ignored."""
    try:
        return bool(matcher.match_file(relative_path))
    except Exception:                              # noqa: BLE001
        return False
