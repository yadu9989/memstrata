"""Phase 34.2 — Deferred background embedding worker.

Drains embedding_queue, computes nomic-embed-text embeddings via Ollama,
stores results in telemetry_timeline_vec.

Hard Rule 69: embedding computation never blocks the ingest path.
Runs as a daemon thread inside the api_server process — no separate OS service.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import NamedTuple

import requests

from memstrata.layer3._db import _load_vec_extension, get_db_path

_logger = logging.getLogger(__name__)

_OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"
_EMBED_MODEL = "nomic-embed-text"
_EMBED_DIM = 768


class _QueueItem(NamedTuple):
    queue_id: int
    timeline_id: int


class EmbeddingWorker:
    """Drains embedding_queue in a background thread.

    Lifecycle: call start() in FastAPI lifespan before yield; call stop() in
    the finally block.  The thread is a daemon so it never prevents clean exit.
    """

    BATCH_SIZE = 16
    POLL_INTERVAL_S = 2.0
    MAX_ATTEMPTS = 3

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._first_boot = True
        self._vec_warned = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="embedding-worker",
            daemon=True,
        )
        self._thread.start()
        _logger.info("[embedding_worker] Started (model=%s, batch=%d)", _EMBED_MODEL, self.BATCH_SIZE)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
        _logger.info("[embedding_worker] Stopped")

    # ------------------------------------------------------------------
    # Thread entry point
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            conn = self._open_conn()
            try:
                if self._first_boot:
                    self._log_backfill_status(conn)
                    self._first_boot = False

                batch = self._get_pending(conn)
                if not batch:
                    conn.close()
                    self._stop.wait(self.POLL_INTERVAL_S)
                    continue

                self._process_batch(conn, batch)
            except Exception as exc:
                _logger.warning("[embedding_worker] Unexpected error in run loop: %s", exc)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Startup log (one-time, per spec Issue 2 decision)
    # ------------------------------------------------------------------

    def _log_backfill_status(self, conn: sqlite3.Connection) -> None:
        total_row = conn.execute("SELECT COUNT(*) FROM embedding_queue").fetchone()
        null_row = conn.execute(
            """
            SELECT COUNT(*) FROM embedding_queue eq
            JOIN telemetry_session_timeline tst ON tst.id = eq.timeline_id
            WHERE tst.chat_session_id IS NULL
            """
        ).fetchone()
        total = total_row[0] if total_row else 0
        null_session = null_row[0] if null_row else 0
        session_scoped = total - null_session
        _logger.info(
            "[embedding_worker] Backfilling %d timeline rows. "
            "%d are scoped to chat_sessions; %d predate Phase 30 and will be "
            "embedded but not retrievable by session scope. This is expected.",
            total, session_scoped, null_session,
        )

    # ------------------------------------------------------------------
    # Queue operations
    # ------------------------------------------------------------------

    def _get_pending(self, conn: sqlite3.Connection) -> list[_QueueItem]:
        rows = conn.execute(
            """
            SELECT id, timeline_id
              FROM embedding_queue
             WHERE completed_at IS NULL
               AND attempts < ?
             ORDER BY id ASC
             LIMIT ?
            """,
            (self.MAX_ATTEMPTS, self.BATCH_SIZE),
        ).fetchall()
        return [_QueueItem(queue_id=r[0], timeline_id=r[1]) for r in rows]

    def _get_text(self, conn: sqlite3.Connection, timeline_id: int) -> str | None:
        row = conn.execute(
            "SELECT text FROM telemetry_session_timeline WHERE id = ?",
            (timeline_id,),
        ).fetchone()
        if not row:
            return None
        text = row[0]
        if not text or not text.strip():
            return None
        return text

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    def _process_batch(self, conn: sqlite3.Connection, batch: list[_QueueItem]) -> None:
        # Separate items with text to embed from empty-text items to skip.
        embeddable: list[tuple[_QueueItem, str]] = []
        for item in batch:
            text = self._get_text(conn, item.timeline_id)
            if text is None:
                # Empty/missing text — mark completed without embedding.
                conn.execute(
                    "UPDATE embedding_queue SET completed_at = datetime('now') WHERE id = ?",
                    (item.queue_id,),
                )
            else:
                embeddable.append((item, text))
        conn.commit()

        if not embeddable:
            return

        texts = [t for _, t in embeddable]
        embeddings = self._embed_batch(texts)

        if embeddings is None:
            # Ollama unavailable — increment attempts; worker retries next poll.
            for item, _ in embeddable:
                conn.execute(
                    """
                    UPDATE embedding_queue
                       SET attempts = attempts + 1,
                           last_error = 'ollama_unavailable'
                     WHERE id = ?
                    """,
                    (item.queue_id,),
                )
            conn.commit()
            return

        # Verify the vec0 table is accessible before attempting writes.
        # If sqlite-vec isn't loaded the INSERT would fail with "no such module: vec0"
        # on every item in the batch.  Bail out early so attempts are not consumed —
        # items stay retryable once sqlite-vec is installed and the server restarts.
        try:
            conn.execute("SELECT 1 FROM telemetry_timeline_vec LIMIT 0")
        except sqlite3.OperationalError as exc:
            if not self._vec_warned:
                _logger.warning(
                    "[embedding_worker] telemetry_timeline_vec not accessible (%s). "
                    "Embeddings paused. Fix with: pip install sqlite-vec",
                    exc,
                )
                self._vec_warned = True
            return

        for (item, _), embedding in zip(embeddable, embeddings):
            if len(embedding) != _EMBED_DIM:
                _logger.warning(
                    "[embedding_worker] timeline_id=%d returned dim %d, expected %d — skipping",
                    item.timeline_id, len(embedding), _EMBED_DIM,
                )
                conn.execute(
                    """
                    UPDATE embedding_queue
                       SET attempts = attempts + 1,
                           last_error = 'wrong_dim'
                     WHERE id = ?
                    """,
                    (item.queue_id,),
                )
                continue

            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO telemetry_timeline_vec (timeline_id, embedding)
                    VALUES (?, ?)
                    """,
                    (item.timeline_id, json.dumps(embedding)),
                )
                conn.execute(
                    "UPDATE embedding_queue SET completed_at = datetime('now') WHERE id = ?",
                    (item.queue_id,),
                )
            except Exception as exc:
                _logger.warning(
                    "[embedding_worker] Store failed for timeline_id=%d: %s",
                    item.timeline_id, exc,
                )
                conn.execute(
                    """
                    UPDATE embedding_queue
                       SET attempts = attempts + 1,
                           last_error = ?
                     WHERE id = ?
                    """,
                    (str(exc)[:500], item.queue_id),
                )

        conn.commit()

    # ------------------------------------------------------------------
    # Ollama HTTP call
    # ------------------------------------------------------------------

    def _embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        """POST to Ollama /api/embed. Returns None on any failure."""
        try:
            resp = requests.post(
                _OLLAMA_EMBED_URL,
                json={"model": _EMBED_MODEL, "input": texts},
                timeout=60.0,
            )
        except Exception as exc:
            _logger.warning("[embedding_worker] Ollama request failed: %s", exc)
            return None

        if not resp.ok:
            _logger.warning(
                "[embedding_worker] Ollama %d: %s",
                resp.status_code, resp.text[:200],
            )
            return None

        try:
            data = resp.json()
            embeddings = data.get("embeddings")
            if not isinstance(embeddings, list) or len(embeddings) != len(texts):
                _logger.warning(
                    "[embedding_worker] Unexpected Ollama response shape: %s",
                    str(data)[:200],
                )
                return None
            return embeddings
        except Exception as exc:
            _logger.warning("[embedding_worker] Failed to parse Ollama response: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Connection factory
    # ------------------------------------------------------------------

    def _open_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(get_db_path()),
            check_same_thread=False,
            timeout=10.0,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        _load_vec_extension(conn)
        return conn
