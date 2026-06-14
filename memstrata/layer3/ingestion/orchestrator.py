"""First-time backfill orchestrator — V5.2-A Phase 35.2.

Four-phase state machine:

  scan    : walk the project tree, count parseable files, write
            indexing_jobs.files_total. Cheap (<30s on a 1.2k-file repo
            per the spec).
  parse   : tree-sitter every file in batches, write code_chunks rows.
            Updates indexing_jobs.files_processed + last_processed_file
            after every batch so a crash resumes from the last completed
            batch (Hard Rule 72).
  embed   : drain the pending-embedding queue. Backend is pluggable;
            ``NoOpEmbedder`` ships zero-vectors for tests, production
            uses ``OllamaEmbedder`` (nomic-embed-text -> 768 dims).
  verify  : sanity-check 10 random chunks are retrievable, mark
            indexing_jobs.phase='complete'.

Hard Rule 70: every phase entry checks ``project_opt_in`` for the
project_path. Without an ``opted_in`` row, ``OptInRequired`` is raised
before any work happens.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from memstrata.layer3.ingestion.chunker import (
    Chunk,
    chunk_file,
    detect_language,
)
from memstrata.layer3.ingestion.denylist import (
    ProjectSkipPolicy,
    load_gitignore,
    should_index,
    should_walk_dir,
)
from memstrata.layer3.ingestion.progress import (
    CONTROL_REGISTRY,
    ControlState,
)
from memstrata.layer3.ingestion.resource_policy import ResourcePolicy

_LOG = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────

PARSE_BATCH_SIZE = 50            # files processed before persisting state
EMBED_BATCH_SIZE = 16            # spec §3.2 Phase C target
EMBED_DIM = 768                  # nomic-embed-text dimensionality
VERIFY_SAMPLE_SIZE = 10          # spec §3.2 Phase D


# ── Job phase enum ────────────────────────────────────────────────────────

class JobPhase:
    """Phase string constants matching the indexing_jobs CHECK constraint."""
    SCAN = "scan"
    PARSE = "parse"
    EMBED = "embed"
    VERIFY = "verify"
    COMPLETE = "complete"
    PAUSED = "paused"
    FAILED = "failed"

    _ALL = frozenset({SCAN, PARSE, EMBED, VERIFY, COMPLETE, PAUSED, FAILED})


@dataclass
class JobState:
    """Read-only view of an indexing_jobs row."""
    id: int
    project_id: str
    project_path: str
    phase: str
    files_total: int
    files_processed: int
    entities_total: int
    entities_embedded: int
    last_processed_file: str | None
    started_at: str
    completed_at: str | None = None
    error: str | None = None


# ── Errors ────────────────────────────────────────────────────────────────

class OptInRequired(RuntimeError):
    """Hard Rule 70: the project_path doesn't have an opted_in row."""


# ── Embedder protocol + default stub ──────────────────────────────────────

class Embedder(Protocol):
    """Convert a batch of texts into 768-dim float embeddings."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class NoOpEmbedder:
    """Deterministic stub embedder for tests + dev environments without Ollama.

    Returns a vector derived from the SHA-256 of the text mapped to the
    [0, 1) range. Vectors are reproducible per input so retrieval tests
    that ask "did we re-embed?" work correctly.
    """

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            # Cycle through the 32-byte digest to fill EMBED_DIM floats.
            vec = [(digest[i % len(digest)] / 255.0) for i in range(EMBED_DIM)]
            out.append(vec)
        return out


# ── Orchestrator ──────────────────────────────────────────────────────────

class BackfillOrchestrator:
    """Drives the four-phase backfill for one project.

    Construct with the DB connection + the absolute project path, then
    call ``run()`` to advance through whatever phase the indexing_jobs
    row is currently in. Resume after crash is automatic — we read
    last_processed_file at parse-phase entry and pick up from the next
    file in the walk order.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        project_path: str | Path,
        embedder: Embedder | None = None,
        clock: Callable[[], float] = time.time,
        skip_policy: ProjectSkipPolicy | None = None,
        resource_policy: ResourcePolicy | None = None,
        control_state: ControlState | None = None,
        respect_gitignore: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.conn = conn
        self.project_id = project_id
        self.project_path = str(Path(project_path).resolve())
        self.embedder: Embedder = embedder or NoOpEmbedder()
        self.clock = clock
        self._abort_after_phase: str | None = None     # test seam — see _maybe_abort
        self._abort_after_batches: int | None = None
        self._batches_processed = 0
        # V5.2-A Phase 35.5: full denylist + gitignore replace the
        # interim _BASE_DENY_DIRS check used by Phase 35.2.
        self.skip_policy = skip_policy or ProjectSkipPolicy()
        self._gitignore = (
            load_gitignore(Path(self.project_path)) if respect_gitignore else None
        )
        # V5.2-A Phase 35.4: resource policy is consulted between embed
        # batches to soft-pause on battery / typing without persisting
        # to indexing_jobs.phase.
        self.resource_policy = resource_policy or ResourcePolicy()
        # V5.2-A Phase 35.3: shared signal block so the API layer can
        # pause / cancel a running orchestrator. ``control_state=None``
        # picks up (or creates) the project's slot in CONTROL_REGISTRY.
        self.control_state = control_state or CONTROL_REGISTRY.get_or_create(project_id)
        self._sleep = sleep
        # Try to load sqlite-vec so the orchestrator can also write to
        # code_chunks_vec. Failure is non-fatal — embed phase just skips
        # the vec insert and the verify phase covers metadata-only mode.
        self._vec_loaded = self._load_sqlite_vec()

    # ── Public API ─────────────────────────────────────────────────────

    def run(self) -> JobState:
        """Run from the current phase to completion.

        Idempotent across crashes: re-entry picks up wherever the
        previous attempt left ``indexing_jobs.phase``.
        """
        self._require_opt_in()
        state = self._ensure_job_row()

        if state.phase in (JobPhase.COMPLETE,):
            return state

        # State machine: each method advances phase + persists.
        if state.phase == JobPhase.SCAN:
            state = self._phase_scan(state)
            self._maybe_abort(JobPhase.SCAN)
        if state.phase == JobPhase.PARSE:
            state = self._phase_parse(state)
            self._maybe_abort(JobPhase.PARSE)
        if state.phase == JobPhase.EMBED:
            state = self._phase_embed(state)
            self._maybe_abort(JobPhase.EMBED)
        if state.phase == JobPhase.VERIFY:
            state = self._phase_verify(state)
        return state

    def current_state(self) -> JobState | None:
        return self._load_state()

    def pause(self) -> None:
        """User-initiated pause — Hard Rule 72."""
        state = self._load_state()
        if state is None or state.phase in (JobPhase.COMPLETE, JobPhase.PAUSED):
            return
        self._update_state(phase=JobPhase.PAUSED)

    def resume(self) -> None:
        """Flip 'paused' -> back to the phase it was paused mid-flight in."""
        state = self._load_state()
        if state is None or state.phase != JobPhase.PAUSED:
            return
        # Best-effort phase recovery: if we have any code_chunks but none
        # have embeddings, we were in EMBED; if files_processed < files_total
        # we were in PARSE; otherwise VERIFY.
        target = JobPhase.PARSE
        if state.files_total > 0 and state.files_processed >= state.files_total:
            target = JobPhase.EMBED if not self._embeddings_complete() else JobPhase.VERIFY
        self._update_state(phase=target)

    # ── Test seam ──────────────────────────────────────────────────────

    def abort_after(self, phase: str, *, batches: int | None = None) -> None:
        """Test hook: raise ``RuntimeError('test-abort')`` partway through.

        Only used in pytest. Production callers never touch this.
        """
        if phase not in JobPhase._ALL:
            raise ValueError(f"unknown phase: {phase}")
        self._abort_after_phase = phase
        self._abort_after_batches = batches
        self._batches_processed = 0

    def _maybe_abort(self, completed_phase: str) -> None:
        if self._abort_after_phase != completed_phase:
            return
        if self._abort_after_batches is not None and self._batches_processed < self._abort_after_batches:
            return
        raise RuntimeError(f"test-abort after phase {completed_phase}")

    # ── Phase A: scan & plan ───────────────────────────────────────────

    def _phase_scan(self, state: JobState) -> JobState:
        files = self._enumerate_files()
        return self._update_state(
            phase=JobPhase.PARSE,
            files_total=len(files),
            files_processed=0,
        )

    def _enumerate_files(self) -> list[str]:
        """Walk the project and return parseable source files (sorted).

        Uses ``denylist.should_walk_dir`` to prune branches at the
        directory level so we never even descend into node_modules /
        .git, and ``denylist.should_index`` for per-file filtering
        (extension, size, binary sniff, gitignore).
        """
        root = Path(self.project_path)
        results: list[str] = []

        def _walk(directory: Path) -> None:
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                return
            for entry in entries:
                if entry.is_symlink():
                    continue   # symlinks across denied dirs cause loops
                if entry.is_dir():
                    if should_walk_dir(entry, root, policy=self.skip_policy).indexed:
                        _walk(entry)
                elif entry.is_file():
                    if detect_language(entry) is None:
                        continue
                    decision = should_index(
                        entry, root,
                        policy=self.skip_policy,
                        gitignore_matcher=self._gitignore,
                    )
                    if decision.indexed:
                        results.append(str(entry))

        _walk(root)
        results.sort()
        return results

    # ── Phase B: AST extraction ────────────────────────────────────────

    def _phase_parse(self, state: JobState) -> JobState:
        files = self._enumerate_files()
        # Resume from where we left off.
        start_idx = 0
        if state.last_processed_file:
            try:
                start_idx = files.index(state.last_processed_file) + 1
            except ValueError:
                start_idx = state.files_processed  # file vanished mid-run

        files_processed = state.files_processed
        entities_total = state.entities_total
        for batch_start in range(start_idx, len(files), PARSE_BATCH_SIZE):
            batch = files[batch_start:batch_start + PARSE_BATCH_SIZE]
            for file_path in batch:
                chunks = chunk_file(file_path)
                entities_total += self._upsert_chunks(file_path, chunks)
                files_processed += 1
            last_file = batch[-1] if batch else state.last_processed_file
            self._update_state(
                files_processed=files_processed,
                entities_total=entities_total,
                last_processed_file=last_file,
            )
            self._batches_processed += 1
            self._maybe_abort(JobPhase.PARSE)
        return self._update_state(phase=JobPhase.EMBED)

    def _upsert_chunks(self, file_path: str, chunks: list[Chunk]) -> int:
        """Insert new chunks; skip rows whose stable_hash matches an
        existing (project_id, stable_hash) entry. Returns count inserted."""
        if not chunks:
            return 0
        inserted = 0
        for chunk in chunks:
            row = self.conn.execute(
                "SELECT id FROM code_chunks WHERE project_id=? AND stable_hash=? LIMIT 1",
                (self.project_id, chunk.stable_hash),
            ).fetchone()
            if row is not None:
                continue
            try:
                self.conn.execute(
                    """
                    INSERT INTO code_chunks
                        (project_id, file_path, language, line_start, line_end,
                         stable_hash, text, token_estimate)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.project_id, chunk.file_path, chunk.language,
                        chunk.line_start, chunk.line_end,
                        chunk.stable_hash, chunk.text, chunk.token_estimate,
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                # UNIQUE (project_id, file_path, line_start, line_end)
                # already covers this region. Skip.
                continue
        # Per-file content hash for branch-switch diffing (Phase 35.8 reads
        # this; we populate it during the initial walk so the watcher
        # has a baseline).
        content_hash = self._hash_file(file_path)
        if content_hash:
            self.conn.execute(
                """
                INSERT INTO file_hashes (project_id, file_path, content_hash)
                VALUES (?, ?, ?)
                ON CONFLICT (project_id, file_path) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    last_seen = CURRENT_TIMESTAMP
                """,
                (self.project_id, file_path, content_hash),
            )
        self.conn.commit()
        return inserted

    @staticmethod
    def _hash_file(path: str) -> str | None:
        try:
            with open(path, "rb") as f:
                h = hashlib.sha256()
                for chunk in iter(lambda: f.read(64 * 1024), b""):
                    h.update(chunk)
                return h.hexdigest()
        except OSError:
            return None

    # ── Phase C: embedding ─────────────────────────────────────────────

    def _phase_embed(self, state: JobState) -> JobState:
        rows = self.conn.execute(
            """
            SELECT c.id, c.text
            FROM code_chunks c
            LEFT JOIN code_chunks_vec v ON v.chunk_id = c.id
            WHERE c.project_id = ? AND v.chunk_id IS NULL
            ORDER BY c.id
            """,
            (self.project_id,),
        ).fetchall() if self._vec_loaded else []

        entities_embedded = state.entities_embedded
        batch_start = 0
        # Soft-pause iterations don't advance batch_start — they just
        # sleep and re-check. Cap them so the test (or a stuck signal)
        # can't make the orchestrator hang forever.
        max_soft_pause_iterations = 200

        while batch_start < len(rows):
            # Hard pause/cancel — Phase 35.3 control flags.
            if self.control_state.cancel_flag.is_set():
                return self._update_state(phase=JobPhase.PAUSED)
            if self.control_state.pause_flag.is_set():
                return self._update_state(phase=JobPhase.PAUSED)

            # Soft pause — Phase 35.4 resource policy.
            pause, reason = self.resource_policy.should_pause_embedding()
            if pause:
                self.control_state.soft_pause_reason = reason
                if max_soft_pause_iterations <= 0:
                    return self._update_state(phase=JobPhase.PAUSED)
                self._sleep(2.0)
                max_soft_pause_iterations -= 1
                continue
            self.control_state.soft_pause_reason = None

            batch = rows[batch_start:batch_start + EMBED_BATCH_SIZE]
            ids = [r[0] for r in batch]
            texts = [r[1] for r in batch]
            try:
                vectors = self.embedder.embed(texts)
            except Exception as exc:                  # noqa: BLE001
                # Hard Rule 64-style: an embedding backend failure
                # parks the job in 'failed' state for the next retry,
                # never crashes the orchestrator.
                return self._update_state(
                    phase=JobPhase.FAILED,
                    error=f"embedder raised: {exc}",
                )
            self._persist_embeddings(ids, vectors)
            entities_embedded += len(ids)
            self._update_state(entities_embedded=entities_embedded)
            self._batches_processed += 1
            self._maybe_abort(JobPhase.EMBED)
            self.control_state.progress_event.set()
            batch_start += EMBED_BATCH_SIZE
        return self._update_state(phase=JobPhase.VERIFY)

    def _persist_embeddings(self, ids: list[int], vectors: list[list[float]]) -> None:
        if not self._vec_loaded:
            return
        # vec0 wants bytes; sqlite-vec ships a helper but we can also use
        # struct directly. Use the helper when available for safety.
        try:
            import sqlite_vec
            serialize = sqlite_vec.serialize_float32        # type: ignore[attr-defined]
        except (ImportError, AttributeError):
            import struct
            def serialize(values: list[float]) -> bytes:
                return struct.pack(f"{len(values)}f", *values)
        for chunk_id, vec in zip(ids, vectors):
            if len(vec) != EMBED_DIM:
                _LOG.warning(
                    "embedder returned %d dims, expected %d; skipping chunk %d",
                    len(vec), EMBED_DIM, chunk_id,
                )
                continue
            try:
                self.conn.execute(
                    "INSERT OR REPLACE INTO code_chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                    (chunk_id, serialize(vec)),
                )
            except sqlite3.OperationalError as exc:
                _LOG.warning("vec0 insert failed for chunk %d: %s", chunk_id, exc)
        self.conn.commit()

    def _embeddings_complete(self) -> bool:
        if not self._vec_loaded:
            return True   # metadata-only mode: nothing to embed
        row = self.conn.execute(
            """
            SELECT COUNT(*) FROM code_chunks c
            LEFT JOIN code_chunks_vec v ON v.chunk_id = c.id
            WHERE c.project_id = ? AND v.chunk_id IS NULL
            """,
            (self.project_id,),
        ).fetchone()
        return (row[0] if row else 0) == 0

    # ── Phase D: verify ────────────────────────────────────────────────

    def _phase_verify(self, state: JobState) -> JobState:
        # Sample N random chunks and confirm they're retrievable. The
        # spec §3.2 phase D just needs a sanity check, not full vector
        # similarity; we verify each sampled chunk has matching metadata
        # row + (when vec is loaded) an embedding entry.
        rows = self.conn.execute(
            """
            SELECT id, stable_hash FROM code_chunks
            WHERE project_id = ?
            ORDER BY RANDOM() LIMIT ?
            """,
            (self.project_id, VERIFY_SAMPLE_SIZE),
        ).fetchall()

        for chunk_id, _ in rows:
            row = self.conn.execute(
                "SELECT id, text FROM code_chunks WHERE id = ?",
                (chunk_id,),
            ).fetchone()
            if row is None or not row[1]:
                return self._update_state(
                    phase=JobPhase.FAILED,
                    error=f"verify: chunk {chunk_id} missing text",
                )
            if self._vec_loaded:
                vrow = self.conn.execute(
                    "SELECT chunk_id FROM code_chunks_vec WHERE chunk_id = ?",
                    (chunk_id,),
                ).fetchone()
                if vrow is None:
                    return self._update_state(
                        phase=JobPhase.FAILED,
                        error=f"verify: chunk {chunk_id} has metadata but no embedding",
                    )

        return self._update_state(
            phase=JobPhase.COMPLETE,
            completed_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    # ── Hard Rule 70 ──────────────────────────────────────────────────

    def _require_opt_in(self) -> None:
        row = self.conn.execute(
            "SELECT state FROM project_opt_in WHERE project_path = ?",
            (self.project_path,),
        ).fetchone()
        if row is None:
            raise OptInRequired(
                f"Hard Rule 70: project_opt_in row missing for {self.project_path}; "
                "call `record_opt_in(...)` first."
            )
        if row[0] != "opted_in":
            raise OptInRequired(
                f"Hard Rule 70: project_opt_in.state == {row[0]!r} for "
                f"{self.project_path}; backfill blocked."
            )

    # ── State persistence ─────────────────────────────────────────────

    def _ensure_job_row(self) -> JobState:
        state = self._load_state()
        if state is not None:
            return state
        self.conn.execute(
            """
            INSERT INTO indexing_jobs
                (project_id, project_path, phase, files_total,
                 files_processed, entities_total, entities_embedded,
                 last_processed_file)
            VALUES (?, ?, ?, 0, 0, 0, 0, NULL)
            """,
            (self.project_id, self.project_path, JobPhase.SCAN),
        )
        self.conn.commit()
        loaded = self._load_state()
        assert loaded is not None
        return loaded

    def _load_state(self) -> JobState | None:
        row = self.conn.execute(
            """
            SELECT id, project_id, project_path, phase, files_total,
                   files_processed, entities_total, entities_embedded,
                   last_processed_file, started_at, completed_at, error
            FROM indexing_jobs WHERE project_id = ?
            """,
            (self.project_id,),
        ).fetchone()
        if row is None:
            return None
        return JobState(*row)

    def _update_state(self, **fields) -> JobState:
        if not fields:
            loaded = self._load_state()
            assert loaded is not None
            return loaded
        valid = {
            "phase", "files_total", "files_processed",
            "entities_total", "entities_embedded",
            "last_processed_file", "completed_at", "error",
        }
        unknown = set(fields) - valid
        if unknown:
            raise ValueError(f"_update_state: unknown fields {unknown}")
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [self.project_id]
        self.conn.execute(
            f"UPDATE indexing_jobs SET {assignments} WHERE project_id = ?",
            values,
        )
        self.conn.commit()
        loaded = self._load_state()
        assert loaded is not None
        return loaded

    # ── sqlite-vec loader ─────────────────────────────────────────────

    def _load_sqlite_vec(self) -> bool:
        try:
            import sqlite_vec
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
            return True
        except Exception as exc:                          # noqa: BLE001
            _LOG.debug("sqlite-vec unavailable for orchestrator: %s", exc)
            return False


# ── Helpers callers reach for ────────────────────────────────────────────

def record_opt_in(
    conn: sqlite3.Connection,
    project_path: str | Path,
    *,
    state: str = "opted_in",
    user_added_dirs: list[str] | None = None,
    user_excluded_dirs: list[str] | None = None,
) -> None:
    """Write the Hard Rule 70 gate row.

    Idempotent: re-recording the same project flips state to the value
    supplied, which is what the wizard's "Never for this project" / "Index"
    buttons want.
    """
    import json
    project_path = str(Path(project_path).resolve())
    if state not in ("opted_in", "opted_out", "pending"):
        raise ValueError(f"invalid opt-in state: {state}")
    conn.execute(
        """
        INSERT INTO project_opt_in (project_path, state, user_added_dirs, user_excluded_dirs)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (project_path) DO UPDATE SET
            state = excluded.state,
            decided_at = CURRENT_TIMESTAMP,
            user_added_dirs = excluded.user_added_dirs,
            user_excluded_dirs = excluded.user_excluded_dirs
        """,
        (
            project_path, state,
            json.dumps(user_added_dirs) if user_added_dirs else None,
            json.dumps(user_excluded_dirs) if user_excluded_dirs else None,
        ),
    )
    conn.commit()


# Phase 35.5 superseded the interim _BASE_DENY_DIRS constant; the full
# denylist now lives in memstrata/layer3/ingestion/denylist.py and is
# consulted via should_walk_dir + should_index from _enumerate_files.
