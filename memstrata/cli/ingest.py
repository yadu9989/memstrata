"""Phase 36 - Codebase ingestion (CLI + library).

Walks a project directory, reads source files, splits them into ~500-token
chunks, embeds each chunk via Ollama's nomic-embed-text, and stores the
results in the `codebase_chunks` + `codebase_chunks_vec` tables. The dashboard
server's /context/injection endpoint reads from these tables to build a real
project-context block instead of the V5.1 stub that always returned "".

Design choices (kept deliberately small):
  - No watch mode; user re-runs the CLI when they want to re-index.
  - File walker uses .gitignore-like skip patterns (vendored, no extra dep).
  - Chunking is fixed-size by character count (TOKENS_PER_CHUNK * 4); good
    enough as a first pass and matches how chat-turn embedding is sized.
  - Re-ingestion is incremental: a file whose SHA-1 hasn't changed is
    skipped; changed files have their old chunks deleted + replaced.
  - Embeddings are best-effort. If Ollama is unreachable the metadata rows
    are still written; the embedding column is just empty until the next
    successful run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import requests

from memstrata.layer3._db import _load_vec_extension, get_db_path, init_db

_logger = logging.getLogger(__name__)

# nomic-embed-text outputs 768-dim vectors; max input ~8192 tokens. Chunk
# to ~500 tokens (2000 chars) with no overlap - simple and fast.
TOKENS_PER_CHUNK = 500
CHARS_PER_TOKEN = 4
CHUNK_CHARS = TOKENS_PER_CHUNK * CHARS_PER_TOKEN
EMBED_BATCH = 8
OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"
EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768

# What we consider "source we'd want context from". Add to taste; the list is
# intentionally narrow so we don't index minified JS, lockfiles, or images.
_INCLUDE_SUFFIXES = {
    ".py", ".pyi",
    ".ts", ".tsx", ".js", ".jsx", ".mjs",
    ".md", ".mdx", ".rst", ".txt",
    ".rs", ".go", ".java", ".kt",
    ".rb", ".php", ".cs", ".swift",
    ".c", ".h", ".cc", ".cpp", ".hpp",
    ".html", ".css", ".scss",
    ".toml", ".yaml", ".yml",
    ".json", ".sql", ".sh", ".ps1",
}

# Directories we never descend into. Kept as a set of names (not full paths)
# so the walker can prune cheaply.
_SKIP_DIRS = {
    ".git", ".hg", ".svn",
    "node_modules", ".venv", "venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", "out", "target", ".next",
    ".tox", ".cache", "coverage",
    ".idea", ".vscode",
    ".memstrata", ".memstrata-pro",
}

# Files we never read even if they have an included suffix.
_SKIP_FILE_PATTERNS = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "uv.lock", "Cargo.lock", "Gemfile.lock", "composer.lock",
    ".vsix",
)

MAX_FILE_BYTES = 1_000_000  # skip files over 1 MB (binary heuristic)


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _FileRef:
    path: Path                 # absolute path on disk
    rel: str                   # path relative to project root, POSIX-style


def iter_source_files(root: Path) -> Iterable[_FileRef]:
    """Yield every source file under *root*, pruning skip-dirs as we go."""
    root = root.resolve()
    for sub in root.rglob("*"):
        # rglob walks lazily but doesn't prune; check ancestors.
        if any(p.name in _SKIP_DIRS for p in sub.parents if p != sub):
            continue
        if not sub.is_file():
            continue
        if sub.name in _SKIP_FILE_PATTERNS:
            continue
        if sub.suffix.lower() not in _INCLUDE_SUFFIXES:
            continue
        try:
            if sub.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        rel = sub.relative_to(root).as_posix()
        yield _FileRef(path=sub, rel=rel)


# ---------------------------------------------------------------------------
# Reading + chunking
# ---------------------------------------------------------------------------

def _read_text(p: Path) -> str | None:
    """Read a file as UTF-8; return None for binary / encoding errors."""
    try:
        return p.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        return None


def chunk_text(text: str, chunk_chars: int = CHUNK_CHARS) -> list[str]:
    """Split text into roughly chunk_chars-sized slices on whitespace boundaries.

    Falls back to a hard split when no whitespace is found within the window
    (e.g., a single very long line of minified code).
    """
    text = text.strip()
    if not text:
        return []
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        end = min(i + chunk_chars, n)
        if end < n:
            # Walk back to the nearest whitespace boundary so a chunk doesn't
            # split a word/identifier; if none found within 100 chars, hard-cut.
            cut = end
            for j in range(end, max(end - 100, i), -1):
                if text[j].isspace():
                    cut = j
                    break
            end = cut
        chunk = text[i:end].strip()
        if chunk:
            out.append(chunk)
        i = end
    return out


def _sha1_hex(s: bytes) -> str:
    return hashlib.sha1(s).hexdigest()


# ---------------------------------------------------------------------------
# Embedding (Ollama nomic-embed-text)
# ---------------------------------------------------------------------------

def _embed_batch(texts: list[str], *, timeout: float = 60.0) -> list[list[float]] | None:
    """POST to Ollama /api/embed. Returns the list of vectors or None on any error."""
    try:
        r = requests.post(
            OLLAMA_EMBED_URL,
            json={"model": EMBED_MODEL, "input": texts},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        emb = data.get("embeddings")
        if emb is None or len(emb) != len(texts):
            _logger.warning("ollama embed returned unexpected shape: %r", data)
            return None
        return emb
    except Exception as exc:
        _logger.warning("ollama embed failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Database I/O
# ---------------------------------------------------------------------------

def _open_conn(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path if db_path else get_db_path()
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    _load_vec_extension(conn)
    init_db(conn)
    return conn


def _existing_sha(conn: sqlite3.Connection, project_id: str, rel: str) -> str | None:
    row = conn.execute(
        "SELECT sha1 FROM codebase_files WHERE project_id = ? AND path = ?",
        (project_id, rel),
    ).fetchone()
    return row["sha1"] if row else None


def _drop_old_chunks(conn: sqlite3.Connection, project_id: str, rel: str) -> None:
    """Remove all existing chunk + vector rows for a (project, path)."""
    ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM codebase_chunks WHERE project_id = ? AND path = ?",
            (project_id, rel),
        ).fetchall()
    ]
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    try:
        conn.execute(
            f"DELETE FROM codebase_chunks_vec WHERE chunk_id IN ({placeholders})",
            ids,
        )
    except sqlite3.OperationalError:
        pass  # vec0 unavailable; nothing to clean
    conn.execute(
        f"DELETE FROM codebase_chunks WHERE id IN ({placeholders})", ids
    )


def _store_chunks(
    conn: sqlite3.Connection,
    project_id: str,
    rel: str,
    chunks: list[str],
    embeddings: list[list[float]] | None,
) -> int:
    """Insert one row per chunk (+ optional embedding). Returns total tokens."""
    total_tokens = 0
    for idx, chunk in enumerate(chunks):
        tokens = max(1, len(chunk) // CHARS_PER_TOKEN)
        total_tokens += tokens
        cur = conn.execute(
            """
            INSERT INTO codebase_chunks (project_id, path, chunk_idx, text, token_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, rel, idx, chunk, tokens),
        )
        chunk_id = cur.lastrowid
        if embeddings is not None and idx < len(embeddings):
            vec = embeddings[idx]
            if len(vec) == EMBED_DIM:
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO codebase_chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                        (chunk_id, json.dumps(vec)),
                    )
                except sqlite3.OperationalError as exc:
                    _logger.warning("vec0 insert failed (chunk_id=%d): %s", chunk_id, exc)
    return total_tokens


def _upsert_file(
    conn: sqlite3.Connection,
    project_id: str,
    rel: str,
    sha1: str,
    size_bytes: int,
    token_count: int,
) -> None:
    conn.execute(
        """
        INSERT INTO codebase_files (project_id, path, sha1, size_bytes, token_count, last_indexed)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT (project_id, path) DO UPDATE SET
            sha1 = excluded.sha1,
            size_bytes = excluded.size_bytes,
            token_count = excluded.token_count,
            last_indexed = excluded.last_indexed
        """,
        (project_id, rel, sha1, size_bytes, token_count),
    )


# ---------------------------------------------------------------------------
# Top-level ingest
# ---------------------------------------------------------------------------

@dataclass
class IngestSummary:
    project_id: str
    root: Path
    files_seen: int
    files_indexed: int
    files_unchanged: int
    files_failed: int
    chunks_written: int
    chunks_embedded: int
    tokens_total: int
    duration_s: float


def ingest_project(
    root: Path,
    *,
    project_id: str | None = None,
    db_path: Path | None = None,
    embed: bool = True,
) -> IngestSummary:
    """Walk *root*, ingest changed source files into the codebase tables.

    project_id defaults to the basename of *root* (so memstrata-pro/
    becomes "memstrata-pro"). Pass an explicit value when the harness or
    extension uses a different identifier.
    """
    start = time.time()
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"not a directory: {root}")

    pid = project_id or root.name
    conn = _open_conn(db_path)

    seen = indexed = unchanged = failed = 0
    chunks_written = chunks_embedded = 0
    tokens_total = 0

    try:
        for ref in iter_source_files(root):
            seen += 1
            raw = ref.path.read_bytes() if ref.path.is_file() else None
            if raw is None:
                failed += 1
                continue
            sha = _sha1_hex(raw)
            if _existing_sha(conn, pid, ref.rel) == sha:
                unchanged += 1
                continue

            text = _read_text(ref.path)
            if text is None:
                failed += 1
                continue

            chunks = chunk_text(text)
            if not chunks:
                # Empty file - still record it as seen so we don't keep retrying.
                _drop_old_chunks(conn, pid, ref.rel)
                _upsert_file(conn, pid, ref.rel, sha, len(raw), 0)
                conn.commit()
                indexed += 1
                continue

            embeddings: list[list[float]] | None = None
            if embed:
                embeddings = []
                for batch_start in range(0, len(chunks), EMBED_BATCH):
                    batch = chunks[batch_start: batch_start + EMBED_BATCH]
                    got = _embed_batch(batch)
                    if got is None:
                        embeddings = None  # bail; store text without vectors
                        break
                    embeddings.extend(got)
                if embeddings is not None:
                    chunks_embedded += len(embeddings)

            _drop_old_chunks(conn, pid, ref.rel)
            written_tokens = _store_chunks(conn, pid, ref.rel, chunks, embeddings)
            _upsert_file(conn, pid, ref.rel, sha, len(raw), written_tokens)
            conn.commit()
            indexed += 1
            chunks_written += len(chunks)
            tokens_total += written_tokens
    finally:
        conn.close()

    duration = round(time.time() - start, 2)
    return IngestSummary(
        project_id=pid,
        root=root,
        files_seen=seen,
        files_indexed=indexed,
        files_unchanged=unchanged,
        files_failed=failed,
        chunks_written=chunks_written,
        chunks_embedded=chunks_embedded,
        tokens_total=tokens_total,
        duration_s=duration,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_ingest(args: argparse.Namespace) -> None:
    """Entry point for `memstrata ingest <path>`."""
    root = Path(args.path).expanduser().resolve()
    if not root.exists():
        print(f"ingest: path does not exist: {root}", file=sys.stderr)
        sys.exit(1)
    if not root.is_dir():
        print(f"ingest: not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    print(f"[memstrata ingest] root: {root}")
    print(f"[memstrata ingest] project_id: {args.project_id or root.name}")
    print(f"[memstrata ingest] embed: {not args.no_embed}")
    print()

    summary = ingest_project(
        root,
        project_id=args.project_id,
        embed=not args.no_embed,
    )

    print(f"  files seen:        {summary.files_seen}")
    print(f"  files indexed:     {summary.files_indexed}")
    print(f"  files unchanged:   {summary.files_unchanged}")
    print(f"  files failed:      {summary.files_failed}")
    print(f"  chunks written:    {summary.chunks_written}")
    print(f"  chunks embedded:   {summary.chunks_embedded}")
    print(f"  tokens total:      {summary.tokens_total:,}")
    print(f"  duration:          {summary.duration_s}s")
    if summary.chunks_embedded == 0 and summary.chunks_written > 0:
        print(
            "\n  ! No embeddings were stored. Ollama at http://localhost:11434 "
            "may be offline. Re-run with the same command after starting it; "
            "unchanged files will be skipped automatically."
        )
