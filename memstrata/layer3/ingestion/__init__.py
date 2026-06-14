"""V5.2-A automated codebase ingestion.

Phase 35 components:
  * Phase 35.1 — ``chunker``    : tree-sitter AST extraction + stable hash.
  * Phase 35.2 — ``orchestrator``: four-phase backfill state machine
                                   (scan -> parse -> embed -> verify).

Hard Rules in scope:
  * #70 — backfill never starts without an explicit ``project_opt_in`` row.
  * #72 — backfill resumes from ``indexing_jobs.last_processed_file`` on
          restart; never "start over from scratch."
"""
from __future__ import annotations

from memstrata.layer3.ingestion.branch_switch import (
    SweepResult,
    sweep_branch_switch,
)
from memstrata.layer3.ingestion.chunker import (
    Chunk,
    chunk_file,
    chunk_source,
    detect_language,
    stable_hash,
)
from memstrata.layer3.ingestion.denylist import (
    DENY_FILE_BASENAMES,
    DENY_FILE_EXTENSIONS,
    HARDCODED_DENYLIST,
    MAX_FILE_SIZE_BYTES,
    SECONDARY_SKIP,
    IndexDecision,
    ProjectSkipPolicy,
    load_gitignore,
    should_index,
    should_walk_dir,
)
from memstrata.layer3.ingestion.lifecycle import (
    IngestionService,
    ProjectRuntime,
)
from memstrata.layer3.ingestion.orchestrator import (
    BackfillOrchestrator,
    JobPhase,
    JobState,
    NoOpEmbedder,
    OptInRequired,
)
from memstrata.layer3.ingestion.progress import (
    CONTROL_REGISTRY,
    ControlState,
    ProgressSnapshot,
    build_snapshot,
)
from memstrata.layer3.ingestion.resource_policy import (
    BatteryState,
    ResourcePolicy,
    apply_cpu_priority,
    current_rss_bytes,
    detect_battery_state,
    detect_idle_seconds,
)
from memstrata.layer3.ingestion.watcher import (
    CodebaseWatcher,
    NotOptedIn,
    ReindexResult,
    reindex_file,
)

__all__ = [
    "Chunk",
    "chunk_file",
    "chunk_source",
    "detect_language",
    "stable_hash",
    "BackfillOrchestrator",
    "JobPhase",
    "JobState",
    "NoOpEmbedder",
    "OptInRequired",
    # Phase 35.5
    "HARDCODED_DENYLIST",
    "SECONDARY_SKIP",
    "DENY_FILE_EXTENSIONS",
    "DENY_FILE_BASENAMES",
    "MAX_FILE_SIZE_BYTES",
    "IndexDecision",
    "ProjectSkipPolicy",
    "should_index",
    "should_walk_dir",
    "load_gitignore",
    # Phase 35.4 / 35.6
    "BatteryState",
    "ResourcePolicy",
    "apply_cpu_priority",
    "current_rss_bytes",
    "detect_battery_state",
    "detect_idle_seconds",
    # Phase 35.3
    "CONTROL_REGISTRY",
    "ControlState",
    "ProgressSnapshot",
    "build_snapshot",
    # Phase 35.7
    "CodebaseWatcher",
    "NotOptedIn",
    "ReindexResult",
    "reindex_file",
    # Phase 35.8
    "SweepResult",
    "sweep_branch_switch",
    # Phase 35.9
    "IngestionService",
    "ProjectRuntime",
]
