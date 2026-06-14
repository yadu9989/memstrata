"""Tree-sitter chunking — V5.2-A Phase 35.1.

Each parseable source file is split into one ``Chunk`` per AST entity
(function, class, method, top-level block). Files that don't parse, or
that are too small to benefit from per-entity granularity (< 50 lines),
collapse to a single file-sized chunk per spec §2.2.

Stable hashing (SHA-256 over normalized source slice) lets the
orchestrator skip re-embedding when the entity's content hasn't actually
changed. Normalization strips trailing whitespace and collapses CRLF
to LF so a `dos2unix` pass doesn't churn every chunk.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger(__name__)

# ── Language detection ────────────────────────────────────────────────────

# Map file extensions to (language_id, grammar_loader). Each loader is a
# zero-arg callable that returns the tree_sitter Language object. We
# load grammars lazily so an import error for one grammar doesn't break
# the others.
_LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
}

# Per-language: AST node types that the chunker emits as chunks. Anything
# not in this list collapses to "other" and rolls up under file-sized
# fallback chunks.
_CHUNK_NODE_TYPES: dict[str, frozenset[str]] = {
    "python": frozenset({
        "function_definition",
        "class_definition",
        "decorated_definition",     # @decorator def / class
    }),
    "javascript": frozenset({
        "function_declaration",
        "class_declaration",
        "method_definition",
        "generator_function_declaration",
        "lexical_declaration",      # const Foo = () => {...}
    }),
    "typescript": frozenset({
        "function_declaration",
        "class_declaration",
        "method_definition",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
        "lexical_declaration",
    }),
    "tsx": frozenset({
        "function_declaration",
        "class_declaration",
        "method_definition",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
        "lexical_declaration",
    }),
}

# Files smaller than this collapse to one file-sized chunk (spec §2.2).
SMALL_FILE_LINE_THRESHOLD = 50

# Files larger than this are split at line-count windows when AST parse
# fails. The window matches the "function-sized 5-100 lines" target in
# §2.2; long unparseable files at least get something indexed.
FALLBACK_WINDOW_LINES = 100


def detect_language(path: str | Path) -> str | None:
    """Return the chunker's language id for *path*, or None if unsupported."""
    suffix = Path(path).suffix.lower()
    return _LANG_BY_EXT.get(suffix)


# ── Tree-sitter grammar loading (lazy + cached) ───────────────────────────

_PARSER_CACHE: dict[str, object] = {}        # language_id -> Parser
_GRAMMAR_LOAD_ERRORS: dict[str, str] = {}


def _get_parser(language: str):
    """Return a tree_sitter.Parser for *language*, or None on import failure."""
    cached = _PARSER_CACHE.get(language)
    if cached is not None:
        return cached
    if language in _GRAMMAR_LOAD_ERRORS:
        return None
    try:
        import tree_sitter as ts
        if language == "python":
            import tree_sitter_python as ts_lang
            lang_capsule = ts_lang.language()
        elif language == "javascript":
            import tree_sitter_javascript as ts_lang
            lang_capsule = ts_lang.language()
        elif language == "typescript":
            import tree_sitter_typescript as ts_lang
            lang_capsule = ts_lang.language_typescript()
        elif language == "tsx":
            import tree_sitter_typescript as ts_lang
            lang_capsule = ts_lang.language_tsx()
        else:
            _GRAMMAR_LOAD_ERRORS[language] = "unknown language"
            return None
        parser = ts.Parser(ts.Language(lang_capsule))
        _PARSER_CACHE[language] = parser
        return parser
    except Exception as exc:                  # noqa: BLE001
        _GRAMMAR_LOAD_ERRORS[language] = str(exc)
        _LOG.warning("Failed to load tree-sitter grammar for %s: %s", language, exc)
        return None


# ── Chunk shape ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Chunk:
    """One unit the embedder consumes.

    ``entity_kind`` is the AST node type ("function_definition", "class",
    "file") so downstream rerankers can prefer certain kinds; ``entity_name``
    is the function / class identifier when we can extract it.
    """
    language: str
    file_path: str
    line_start: int          # 1-indexed, inclusive
    line_end: int            # 1-indexed, inclusive
    text: str
    stable_hash: str
    entity_kind: str
    entity_name: str | None = None
    token_estimate: int = 0
    # Helpful for tests; not stored in DB.
    metadata: dict = field(default_factory=dict)


# ── Stable hash ───────────────────────────────────────────────────────────

def stable_hash(text: str) -> str:
    """SHA-256 hex digest over normalized text.

    Normalization:
      * CRLF -> LF (so a CRLF-only conversion pass doesn't churn every chunk)
      * Strip trailing whitespace per line (auto-formatters that strip
        trailing spaces shouldn't trigger re-embeds)
      * Strip leading/trailing blank lines (preserves intent-bearing
        internal blank lines)

    We deliberately do NOT strip comments or normalize identifier
    names — those carry semantic signal the embedder uses.
    """
    normalized_lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    # Strip leading/trailing fully-blank lines.
    while normalized_lines and not normalized_lines[0]:
        normalized_lines.pop(0)
    while normalized_lines and not normalized_lines[-1]:
        normalized_lines.pop()
    normalized = "\n".join(normalized_lines)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _estimate_tokens(text: str) -> int:
    """Char-based token estimate (~3.5 chars/token average)."""
    return max(1, int(len(text) / 3.5))


# ── Per-language entity name extraction ───────────────────────────────────

def _entity_name_python(node) -> str | None:
    # function_definition / class_definition both have an `identifier`
    # child named "name" in the Python grammar.
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return name_node.text.decode("utf-8", errors="replace") if hasattr(name_node, "text") else None
    return None


def _entity_name_js_ts(node) -> str | None:
    """JS/TS: function_declaration / class_declaration / method_definition all
    have a name child. lexical_declaration (e.g. `const Foo = ...`) names
    appear under variable_declarator -> identifier."""
    name_node = node.child_by_field_name("name")
    if name_node is not None and hasattr(name_node, "text"):
        return name_node.text.decode("utf-8", errors="replace")
    # lexical_declaration: dig into the first variable_declarator.
    for child in node.children:
        if child.type == "variable_declarator":
            id_child = child.child_by_field_name("name")
            if id_child is not None and hasattr(id_child, "text"):
                return id_child.text.decode("utf-8", errors="replace")
    return None


def _entity_name(language: str, node) -> str | None:
    if language == "python":
        return _entity_name_python(node)
    return _entity_name_js_ts(node)


# ── Chunking ──────────────────────────────────────────────────────────────

def _file_fallback_chunks(language: str, file_path: str, source: str) -> list[Chunk]:
    """Whole-file or windowed-fallback chunks (no AST entities)."""
    lines = source.split("\n")
    n = len(lines)
    if n == 0:
        return []

    if n <= SMALL_FILE_LINE_THRESHOLD:
        # Tiny file: one chunk for the whole thing.
        text = "\n".join(lines)
        return [Chunk(
            language=language, file_path=file_path,
            line_start=1, line_end=n,
            text=text,
            stable_hash=stable_hash(text),
            entity_kind="file",
            entity_name=None,
            token_estimate=_estimate_tokens(text),
        )]

    # Large unparseable file: slice into FALLBACK_WINDOW_LINES windows.
    chunks: list[Chunk] = []
    for window_idx, start in enumerate(range(0, n, FALLBACK_WINDOW_LINES)):
        end = min(start + FALLBACK_WINDOW_LINES, n)
        window_text = "\n".join(lines[start:end])
        if not window_text.strip():
            continue
        chunks.append(Chunk(
            language=language, file_path=file_path,
            line_start=start + 1, line_end=end,
            text=window_text,
            stable_hash=stable_hash(window_text),
            entity_kind="file_window",
            entity_name=f"window_{window_idx}",
            token_estimate=_estimate_tokens(window_text),
        ))
    return chunks


def chunk_source(language: str, file_path: str, source: str) -> list[Chunk]:
    """Split *source* into Chunks using *language*'s tree-sitter grammar.

    Falls back to whole-file or windowed chunks when:
      * The grammar isn't loadable.
      * The parser returns a root with `has_error` covering the whole file.
      * The file is < SMALL_FILE_LINE_THRESHOLD lines (spec §2.2).
    """
    line_count = source.count("\n") + (0 if source.endswith("\n") else 1)
    if line_count <= SMALL_FILE_LINE_THRESHOLD:
        return _file_fallback_chunks(language, file_path, source)

    target_node_types = _CHUNK_NODE_TYPES.get(language)
    if target_node_types is None:
        return _file_fallback_chunks(language, file_path, source)

    parser = _get_parser(language)
    if parser is None:
        return _file_fallback_chunks(language, file_path, source)

    try:
        tree = parser.parse(source.encode("utf-8"))
    except Exception as exc:                  # noqa: BLE001
        _LOG.debug("tree-sitter parse failed for %s: %s", file_path, exc)
        return _file_fallback_chunks(language, file_path, source)

    root = tree.root_node
    chunks: list[Chunk] = []

    def _emit(node) -> None:
        # tree-sitter Point is 0-indexed; the spec wants 1-indexed lines.
        line_start = node.start_point[0] + 1
        line_end = node.end_point[0] + 1
        if line_end < line_start:
            return
        text = source.encode("utf-8")[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        if not text.strip():
            return
        chunks.append(Chunk(
            language=language, file_path=file_path,
            line_start=line_start, line_end=line_end,
            text=text,
            stable_hash=stable_hash(text),
            entity_kind=node.type,
            entity_name=_entity_name(language, node),
            token_estimate=_estimate_tokens(text),
        ))

    # JS/TS wrap declarations in `export_statement` nodes; Python doesn't
    # have an equivalent wrapper. Descending into export wrappers gives
    # us the same per-entity granularity for both.
    _WRAPPER_NODE_TYPES = {"export_statement"}

    def _candidate_nodes(parent) -> list:
        out: list = []
        for child in parent.children:
            if child.type in _WRAPPER_NODE_TYPES:
                out.extend(_candidate_nodes(child))
            else:
                out.append(child)
        return out

    # Walk only the top-level children (plus export wrappers' children)
    # + one level inside class bodies so methods get their own chunk.
    # Anything deeper would explode chunk counts without adding signal.
    for child in _candidate_nodes(root):
        if child.type in target_node_types:
            _emit(child)
            # If it's a class definition, also emit its methods as separate
            # chunks so retrieval can hit a single method directly.
            body = child.child_by_field_name("body")
            if body is not None:
                for grandchild in body.children:
                    if grandchild.type in {"function_definition", "method_definition"}:
                        _emit(grandchild)

    if not chunks:
        # AST didn't surface anything chunkable (e.g. file is all top-level
        # statements). Fall back rather than returning empty.
        return _file_fallback_chunks(language, file_path, source)
    return chunks


def chunk_file(path: str | Path) -> list[Chunk]:
    """Convenience wrapper that reads *path* + dispatches to chunk_source."""
    path = Path(path)
    language = detect_language(path)
    if language is None:
        return []
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _LOG.debug("chunk_file: read failed for %s: %s", path, exc)
        return []
    return chunk_source(language, str(path), source)
