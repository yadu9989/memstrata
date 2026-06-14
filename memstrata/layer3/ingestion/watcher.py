"""File system watcher — V5.2-A Phase 35.7.

A long-lived watcher that listens for source file edits inside a single
opted-in project root and incrementally re-chunks + re-embeds only the
entities whose content actually changed.

Architecture (matches V5_2_A_ADDENDUM §5):

  watchdog Observer (one OS thread)
    -> FileSystemEventHandler
       -> _enqueue(path)   (lock-protected pending map: path -> last_seen)
    drain thread (Python)
       -> wake every DRAIN_INTERVAL_SECONDS
       -> pick paths whose last_seen is older than DEBOUNCE_SECONDS
       -> per path: should_index gate, chunk_file, diff vs DB by
          stable_hash, write deltas to code_chunks + code_chunks_vec.

Hard Rules:
  * #73  — watcher scope is strictly the opted-in project root. The
           constructor refuses to start without a project_opt_in row
           in state='opted_in'.
  * §5.2 — 500 ms debounce so a refactor that saves N files in 200 ms
           still only fires N re-chunk passes, not 5N.

Diff-by-stable-hash semantics:

  Each file save triggers one ``reindex_file`` call.

    existing_hashes  = current code_chunks for this file_path
    new_hashes       = chunks from chunk_file(path)
    to_add           = chunks in new_hashes - existing_hashes
    to_remove        = code_chunks row IDs whose stable_hash is gone
    unchanged        = the intersection -> no work, no embedding

  This is correct because an entity's stable_hash IS its content
  fingerprint: same hash means same chunk text means same embedding.
  A renamed-but-untouched function produces an identical hash and
  shouldn't be re-embedded; a one-character body edit produces a
  different hash and triggers a full delete + insert + embed.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from memstrata.layer3.ingestion.chunker import (
    Chunk,
    chunk_file,
    detect_language,
)
from memstrata.layer3.ingestion.denylist import (
    ProjectSkipPolicy,
    load_gitignore,
    should_index,
)

_LOG = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 0.5          # 500 ms per §5.2
DRAIN_INTERVAL_SECONDS = 0.1    # wake the drain thread every 100 ms


# ── Embedder protocol (mirrors orchestrator) ──────────────────────────────

class Embedder:                                              # pragma: no cover — interface
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


# ── Hard Rule 73 ─────────────────────────────────────────────────────────

class NotOptedIn(RuntimeError):
    """Project path lacks a project_opt_in row in 'opted_in' state."""


def _require_opt_in(conn: sqlite3.Connection, project_path: str) -> None:
    row = conn.execute(
        "SELECT state FROM project_opt_in WHERE project_path = ?",
        (project_path,),
    ).fetchone()
    if row is None:
        raise NotOptedIn(
            f"Hard Rule 73: watcher refused — project_opt_in row missing for {project_path!r}"
        )
    if row[0] != "opted_in":
        raise NotOptedIn(
            f"Hard Rule 73: watcher refused — project_opt_in.state={row[0]!r} for {project_path!r}"
        )


# ── Incremental reindex (used by drain thread + tests) ────────────────────

@dataclass(frozen=True)
class ReindexResult:
    """What changed for a single file. Returned for tests + telemetry."""
    file_path: str
    added: int = 0
    removed: int = 0
    unchanged: int = 0
    embedded: int = 0
    deleted_from_vec: int = 0
    skipped_reason: str | None = None
    # File was discovered missing on disk (e.g. moved or deleted).
    file_missing: bool = False


def _file_content_hash(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def reindex_file(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: Path,
    file_path: Path,
    *,
    embedder: Embedder | None = None,
    skip_policy: ProjectSkipPolicy | None = None,
    gitignore_matcher: object | None = None,
    vec_loaded: bool | None = None,
) -> ReindexResult:
    """Diff one file against its existing code_chunks rows.

    Called from the watcher's drain thread, and exposed at module-level
    so tests can drive incremental updates without spinning up the
    watcher's threads.
    """
    file_path_str = str(file_path)
    policy = skip_policy or ProjectSkipPolicy()

    # Step 1: HR 71 denylist + size + binary + gitignore filter.
    decision = should_index(
        file_path, project_root, policy=policy, gitignore_matcher=gitignore_matcher,
    )
    if not decision.indexed:
        # Removed files trigger a different code path (handled below).
        if decision.reason in ("stat-failed:[Errno 2] No such file or directory", "empty-file"):
            return _handle_file_disappeared(conn, project_id, file_path_str, decision.reason)
        if not file_path.exists():
            return _handle_file_disappeared(conn, project_id, file_path_str, "missing")
        return ReindexResult(file_path=file_path_str, skipped_reason=decision.reason)

    if detect_language(file_path) is None:
        return ReindexResult(file_path=file_path_str, skipped_reason="unsupported-language")

    if not file_path.exists():
        return _handle_file_disappeared(conn, project_id, file_path_str, "missing")

    new_chunks = chunk_file(file_path)

    existing_rows = conn.execute(
        """
        SELECT id, stable_hash FROM code_chunks
        WHERE project_id = ? AND file_path = ?
        """,
        (project_id, file_path_str),
    ).fetchall()
    existing_by_hash: dict[str, int] = {row[1]: row[0] for row in existing_rows}
    new_by_hash: dict[str, Chunk] = {chunk.stable_hash: chunk for chunk in new_chunks}

    existing_hash_set = set(existing_by_hash)
    new_hash_set = set(new_by_hash)

    to_add_hashes = new_hash_set - existing_hash_set
    to_remove_hashes = existing_hash_set - new_hash_set
    unchanged_hashes = existing_hash_set & new_hash_set

    # Step 2: Remove vanished chunks.
    removed_ids: list[int] = []
    for h in to_remove_hashes:
        cid = existing_by_hash[h]
        removed_ids.append(cid)
    if removed_ids:
        placeholders = ",".join("?" * len(removed_ids))
        conn.execute(
            f"DELETE FROM code_chunks WHERE id IN ({placeholders})",
            removed_ids,
        )

    # Step 3: Insert new chunks. Catch the (project_id, file_path,
    # line_start, line_end) UNIQUE constraint so an unrelated overlap
    # doesn't break the watcher.
    added_chunks: list[tuple[int, str]] = []
    for h in to_add_hashes:
        chunk = new_by_hash[h]
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO code_chunks
                (project_id, file_path, language, line_start, line_end,
                 stable_hash, text, token_estimate)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id, chunk.file_path, chunk.language,
                chunk.line_start, chunk.line_end,
                chunk.stable_hash, chunk.text, chunk.token_estimate,
            ),
        )
        if cur.lastrowid:
            added_chunks.append((cur.lastrowid, chunk.text))

    # Step 4: Update file_hashes for branch-switch diffing.
    content_hash = _file_content_hash(file_path)
    if content_hash:
        conn.execute(
            """
            INSERT INTO file_hashes (project_id, file_path, content_hash)
            VALUES (?, ?, ?)
            ON CONFLICT (project_id, file_path) DO UPDATE SET
                content_hash = excluded.content_hash,
                last_seen = CURRENT_TIMESTAMP
            """,
            (project_id, file_path_str, content_hash),
        )

    conn.commit()

    # Step 5: Vec0 deltas. We do this AFTER the metadata commit so the
    # code_chunks_vec row can't outlive its metadata row.
    deleted_from_vec = 0
    if vec_loaded is None:
        vec_loaded = _try_load_vec(conn)
    if vec_loaded and removed_ids:
        placeholders = ",".join("?" * len(removed_ids))
        try:
            conn.execute(
                f"DELETE FROM code_chunks_vec WHERE chunk_id IN ({placeholders})",
                removed_ids,
            )
            deleted_from_vec = len(removed_ids)
        except sqlite3.OperationalError as exc:
            _LOG.debug("vec0 delete failed: %s", exc)

    embedded = 0
    if vec_loaded and added_chunks and embedder is not None:
        try:
            vectors = embedder.embed([text for _, text in added_chunks])
            _persist_embeddings(conn, [cid for cid, _ in added_chunks], vectors)
            embedded = len(added_chunks)
        except Exception as exc:                          # noqa: BLE001 — HR 64 posture
            _LOG.warning("watcher embedding batch failed: %s", exc)

    conn.commit()
    return ReindexResult(
        file_path=file_path_str,
        added=len(to_add_hashes),
        removed=len(removed_ids),
        unchanged=len(unchanged_hashes),
        embedded=embedded,
        deleted_from_vec=deleted_from_vec,
    )


def _handle_file_disappeared(
    conn: sqlite3.Connection,
    project_id: str,
    file_path: str,
    reason: str,
) -> ReindexResult:
    """Delete every code_chunks + vec entry for a path that no longer exists."""
    rows = conn.execute(
        "SELECT id FROM code_chunks WHERE project_id = ? AND file_path = ?",
        (project_id, file_path),
    ).fetchall()
    if not rows:
        return ReindexResult(file_path=file_path, file_missing=True, skipped_reason=reason)
    ids = [r[0] for r in rows]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"DELETE FROM code_chunks WHERE id IN ({placeholders})", ids,
    )
    deleted_from_vec = 0
    if _try_load_vec(conn):
        try:
            conn.execute(
                f"DELETE FROM code_chunks_vec WHERE chunk_id IN ({placeholders})", ids,
            )
            deleted_from_vec = len(ids)
        except sqlite3.OperationalError as exc:
            _LOG.debug("vec0 delete on missing file failed: %s", exc)
    conn.execute(
        "DELETE FROM file_hashes WHERE project_id = ? AND file_path = ?",
        (project_id, file_path),
    )
    conn.commit()
    return ReindexResult(
        file_path=file_path,
        removed=len(ids),
        deleted_from_vec=deleted_from_vec,
        file_missing=True,
        skipped_reason=reason,
    )


def _try_load_vec(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:                                      # noqa: BLE001
        return False


def _persist_embeddings(
    conn: sqlite3.Connection,
    chunk_ids: list[int],
    vectors: list[list[float]],
) -> None:
    try:
        import sqlite_vec
        serialize = sqlite_vec.serialize_float32       # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        import struct
        def serialize(values: list[float]) -> bytes:
            return struct.pack(f"{len(values)}f", *values)
    for chunk_id, vec in zip(chunk_ids, vectors):
        if len(vec) != 768:
            continue
        try:
            conn.execute(
                "INSERT OR REPLACE INTO code_chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, serialize(vec)),
            )
        except sqlite3.OperationalError as exc:
            _LOG.warning("vec0 insert failed for chunk %s: %s", chunk_id, exc)


# ── Watcher class ────────────────────────────────────────────────────────

class CodebaseWatcher:
    """One watcher per opted-in project root.

    Thread shape:
      * watchdog Observer thread feeds ``_on_event`` (lock-protected).
      * One drain thread (started in ``start()``) wakes every
        DRAIN_INTERVAL_SECONDS and processes paths whose last_seen is
        older than DEBOUNCE_SECONDS.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        project_root: str | Path,
        embedder: Embedder | None = None,
        skip_policy: ProjectSkipPolicy | None = None,
        respect_gitignore: bool = True,
        clock: Callable[[], float] = time.monotonic,
        debounce_seconds: float = DEBOUNCE_SECONDS,
    ) -> None:
        self.conn = conn
        self.project_id = project_id
        self.project_root = Path(project_root).resolve()
        self.embedder = embedder
        self.skip_policy = skip_policy or ProjectSkipPolicy()
        self._gitignore = load_gitignore(self.project_root) if respect_gitignore else None
        self.clock = clock
        self.debounce_seconds = debounce_seconds

        # Hard Rule 73: refuse construction unless the project is
        # explicitly opted in. We check at construct time so a misconfigured
        # caller can't even hold a watcher handle.
        _require_opt_in(self.conn, str(self.project_root))

        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._observer = None
        self._drain_thread: threading.Thread | None = None
        self._vec_loaded = _try_load_vec(self.conn)
        # Stats; surfaced via ``stats`` for tests + UI hookups.
        self._processed_count = 0
        self._last_result: ReindexResult | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin watching. Raises if already running."""
        if self._observer is not None:
            raise RuntimeError("CodebaseWatcher already started")
        try:
            from watchdog.events import (
                FileCreatedEvent,
                FileDeletedEvent,
                FileModifiedEvent,
                FileMovedEvent,
                FileSystemEventHandler,
            )
            from watchdog.observers import Observer
        except ImportError as exc:                         # pragma: no cover
            raise RuntimeError("watchdog is required for CodebaseWatcher") from exc

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):    # type: ignore[override]
                if not event.is_directory:
                    watcher._enqueue(event.src_path)

            def on_modified(self, event):   # type: ignore[override]
                if not event.is_directory:
                    watcher._enqueue(event.src_path)

            def on_deleted(self, event):    # type: ignore[override]
                if not event.is_directory:
                    watcher._enqueue(event.src_path)

            def on_moved(self, event):      # type: ignore[override]
                if not event.is_directory:
                    watcher._enqueue(event.src_path)
                    watcher._enqueue(event.dest_path)

        self._observer = Observer()
        self._observer.schedule(_Handler(), str(self.project_root), recursive=True)
        self._observer.start()

        self._drain_thread = threading.Thread(
            target=self._drain_loop, daemon=True, name=f"watcher-drain-{self.project_id}",
        )
        self._drain_thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop the observer + drain threads. Idempotent."""
        self._stop_event.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=timeout)
            except Exception:                              # noqa: BLE001
                pass
            self._observer = None
        if self._drain_thread is not None:
            self._drain_thread.join(timeout=timeout)
            self._drain_thread = None

    # ── Internal seam ──────────────────────────────────────────────────

    def _enqueue(self, raw_path: str) -> None:
        """Watchdog handler thread -> pending map."""
        try:
            path = Path(raw_path).resolve()
        except OSError:
            return
        with self._lock:
            self._pending[str(path)] = self.clock()

    def _drain_loop(self) -> None:                         # pragma: no cover — thread loop
        while not self._stop_event.wait(DRAIN_INTERVAL_SECONDS):
            self.drain_pending(self.clock())

    # ── Test seam ──────────────────────────────────────────────────────

    def feed_event(self, path: str) -> None:
        """Inject a fake watchdog event. Tests use this in place of a
        real Observer; production code goes through ``_enqueue`` from
        the watchdog handler thread."""
        self._enqueue(path)

    def drain_pending(self, now: float | None = None) -> list[ReindexResult]:
        """Process every settled path. Exposed for test determinism;
        the production drain thread calls this on a timer."""
        now = now if now is not None else self.clock()
        with self._lock:
            settled = [
                p for p, last_seen in self._pending.items()
                if (now - last_seen) >= self.debounce_seconds
            ]
            for p in settled:
                del self._pending[p]

        results: list[ReindexResult] = []
        for raw_path in settled:
            try:
                path = Path(raw_path)
                result = reindex_file(
                    self.conn,
                    self.project_id,
                    self.project_root,
                    path,
                    embedder=self.embedder,
                    skip_policy=self.skip_policy,
                    gitignore_matcher=self._gitignore,
                    vec_loaded=self._vec_loaded,
                )
            except Exception as exc:                       # noqa: BLE001 — HR 64 posture
                _LOG.exception("watcher reindex failed for %s: %s", raw_path, exc)
                result = ReindexResult(file_path=raw_path, skipped_reason=f"crashed: {exc}")
            results.append(result)
            self._processed_count += 1
            self._last_result = result
        return results

    # ── Observability ─────────────────────────────────────────────────

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def processed_count(self) -> int:
        return self._processed_count

    @property
    def last_result(self) -> ReindexResult | None:
        return self._last_result
