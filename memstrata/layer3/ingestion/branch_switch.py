"""Branch-switch / mass-mutation sweep — V5.2-A Phase 35.8.

Triggered when the watcher couldn't see what happened — process restart,
git checkout of a different branch, mass mv / rm, bulk patch application.
The sweep diffs every parseable file in the project against the
``file_hashes`` table and routes each non-trivial change through
``reindex_file`` so the diff-by-stable-hash logic from Phase 35.7 owns
the actual chunk update.

Categories produced:

  new        : file exists on disk, no row in file_hashes
  modified   : file_hashes.content_hash != current SHA-256 of bytes
  deleted    : file_hashes row exists, file missing on disk
  unchanged  : hashes match -> no work, no DB write, no embedding

Reuses ``should_index`` for the workspace scan so a switched-to branch
that adds a node_modules/ tree doesn't blow up indexing time.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from memstrata.layer3.ingestion.chunker import detect_language
from memstrata.layer3.ingestion.denylist import (
    ProjectSkipPolicy,
    load_gitignore,
    should_index,
    should_walk_dir,
)
from memstrata.layer3.ingestion.watcher import (
    Embedder,
    ReindexResult,
    reindex_file,
)

_LOG = logging.getLogger(__name__)


# ── Result ────────────────────────────────────────────────────────────────

@dataclass
class SweepResult:
    """Summary returned by sweep_branch_switch.

    Counts are for the per-FILE outcome (not per-chunk). chunks_*
    aggregate across all reindex_file calls so the UI can show a single
    "applied X chunk changes" line.
    """
    new_files: int = 0
    modified_files: int = 0
    deleted_files: int = 0
    unchanged_files: int = 0
    skipped_files: int = 0
    chunks_added: int = 0
    chunks_removed: int = 0
    chunks_unchanged: int = 0
    chunks_embedded: int = 0
    elapsed_seconds: float = 0.0
    # Per-file ReindexResult records — useful for tests + debug overlay;
    # production callers ignore this.
    file_results: list[ReindexResult] = None       # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.file_results is None:
            self.file_results = []


# ── File enumeration (shared shape with the orchestrator) ────────────────

def _enumerate_files(
    root: Path,
    *,
    policy: ProjectSkipPolicy,
    gitignore_matcher: object | None,
) -> list[Path]:
    """Recursive walk that yields every PARSEABLE file under root.

    Identical logic to BackfillOrchestrator._enumerate_files but stays
    in this module so the sweep doesn't depend on private orchestrator
    internals.
    """
    out: list[Path] = []

    def _walk(directory: Path) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if should_walk_dir(entry, root, policy=policy).indexed:
                    _walk(entry)
            elif entry.is_file():
                if detect_language(entry) is None:
                    continue
                decision = should_index(
                    entry, root,
                    policy=policy,
                    gitignore_matcher=gitignore_matcher,
                )
                if decision.indexed:
                    out.append(entry)

    _walk(root)
    return out


def _file_sha256(path: Path) -> str | None:
    """SHA-256 of file bytes, or None on read failure."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# ── Sweep ────────────────────────────────────────────────────────────────

def sweep_branch_switch(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str | Path,
    *,
    embedder: Embedder | None = None,
    skip_policy: ProjectSkipPolicy | None = None,
    respect_gitignore: bool = True,
) -> SweepResult:
    """Walk *project_root*, diff against ``file_hashes``, dispatch changes.

    Returns a ``SweepResult`` summarizing how many files moved into each
    category. The actual chunk-level work happens inside ``reindex_file``;
    we just decide which files need to visit it.
    """
    started = time.monotonic()
    root = Path(project_root).resolve()
    policy = skip_policy or ProjectSkipPolicy()
    gitignore = load_gitignore(root) if respect_gitignore else None

    # ── Pull current workspace hashes ──────────────────────────────────────
    current_files = _enumerate_files(root, policy=policy, gitignore_matcher=gitignore)
    current_hashes: dict[str, str] = {}
    for path in current_files:
        h = _file_sha256(path)
        if h is not None:
            current_hashes[str(path)] = h

    # ── Pull previously-known hashes ───────────────────────────────────────
    rows = conn.execute(
        "SELECT file_path, content_hash FROM file_hashes WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    previous_hashes: dict[str, str] = {row[0]: row[1] for row in rows}

    current_paths = set(current_hashes)
    previous_paths = set(previous_hashes)

    new_paths = current_paths - previous_paths
    deleted_paths = previous_paths - current_paths
    common_paths = current_paths & previous_paths

    result = SweepResult()

    # ── Modified vs unchanged within the common set ───────────────────────
    modified_paths: list[str] = []
    for path_str in common_paths:
        if current_hashes[path_str] != previous_hashes[path_str]:
            modified_paths.append(path_str)
        else:
            result.unchanged_files += 1

    # ── Dispatch ──────────────────────────────────────────────────────────
    # Sort each bucket for deterministic test ordering + reproducible logs.
    for path_str in sorted(new_paths):
        sub = reindex_file(
            conn, project_id, root, Path(path_str),
            embedder=embedder, skip_policy=policy,
            gitignore_matcher=gitignore,
        )
        result.file_results.append(sub)
        if sub.skipped_reason:
            result.skipped_files += 1
        else:
            result.new_files += 1
            result.chunks_added += sub.added
            result.chunks_removed += sub.removed
            result.chunks_unchanged += sub.unchanged
            result.chunks_embedded += sub.embedded

    for path_str in sorted(modified_paths):
        sub = reindex_file(
            conn, project_id, root, Path(path_str),
            embedder=embedder, skip_policy=policy,
            gitignore_matcher=gitignore,
        )
        result.file_results.append(sub)
        if sub.skipped_reason:
            result.skipped_files += 1
        else:
            result.modified_files += 1
            result.chunks_added += sub.added
            result.chunks_removed += sub.removed
            result.chunks_unchanged += sub.unchanged
            result.chunks_embedded += sub.embedded

    for path_str in sorted(deleted_paths):
        sub = reindex_file(
            conn, project_id, root, Path(path_str),
            embedder=embedder, skip_policy=policy,
            gitignore_matcher=gitignore,
        )
        result.file_results.append(sub)
        if sub.file_missing:
            result.deleted_files += 1
            result.chunks_removed += sub.removed

    result.elapsed_seconds = time.monotonic() - started
    return result
