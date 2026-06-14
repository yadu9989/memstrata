"""Tests for memstrata.layer3.ingestion.chunker (V5.2-A Phase 35.1)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from memstrata.layer3.ingestion.chunker import (
    SMALL_FILE_LINE_THRESHOLD,
    Chunk,
    chunk_file,
    chunk_source,
    detect_language,
    stable_hash,
)

# ── Language detection ────────────────────────────────────────────────────

class TestDetectLanguage:
    @pytest.mark.parametrize("path,expected", [
        ("a.py", "python"),
        ("a.pyi", "python"),
        ("a.js", "javascript"),
        ("a.mjs", "javascript"),
        ("a.jsx", "javascript"),
        ("a.ts", "typescript"),
        ("a.tsx", "tsx"),
        ("a.txt", None),
        ("Makefile", None),
        ("a.PY", "python"),         # case-insensitive
    ])
    def test_extension_mapping(self, path, expected):
        assert detect_language(path) == expected


# ── Stable hash ───────────────────────────────────────────────────────────

class TestStableHash:
    def test_deterministic(self):
        assert stable_hash("def foo(): pass\n") == stable_hash("def foo(): pass\n")

    def test_different_content_different_hash(self):
        assert stable_hash("def foo(): pass") != stable_hash("def bar(): pass")

    def test_trailing_whitespace_normalized(self):
        a = "def foo():\n    return 1\n"
        b = "def foo():   \n    return 1   \n"
        assert stable_hash(a) == stable_hash(b)

    def test_crlf_to_lf_normalized(self):
        a = "def foo():\n    return 1\n"
        b = "def foo():\r\n    return 1\r\n"
        assert stable_hash(a) == stable_hash(b)

    def test_leading_trailing_blank_lines_stripped(self):
        a = "def foo(): pass"
        b = "\n\n\ndef foo(): pass\n\n\n"
        assert stable_hash(a) == stable_hash(b)

    def test_internal_blank_lines_preserved(self):
        a = "def foo(): pass\n\ndef bar(): pass"
        b = "def foo(): pass\ndef bar(): pass"
        assert stable_hash(a) != stable_hash(b)

    def test_hash_is_sha256_hex(self):
        digest = stable_hash("x")
        assert len(digest) == 64
        int(digest, 16)  # raises if non-hex


# ── Small files collapse to file-sized chunks ────────────────────────────

class TestSmallFileFallback:
    def test_tiny_python_file_is_one_chunk(self):
        src = "def hello():\n    return 'world'\n"
        chunks = chunk_source("python", "a.py", src)
        assert len(chunks) == 1
        assert chunks[0].entity_kind == "file"
        assert chunks[0].line_start == 1

    def test_threshold_constant_matches_spec(self):
        # V5_2_A_ADDENDUM §2.2 says <50 lines collapses to file chunk.
        assert SMALL_FILE_LINE_THRESHOLD == 50


# ── Python AST chunking ──────────────────────────────────────────────────

class TestPythonChunker:
    @pytest.fixture
    def big_python_source(self):
        # 45 header lines + functions + class so we exceed the 50-line
        # threshold and exercise the AST path.
        return (
            "# header\n" * 45 +
            textwrap.dedent("""
                def add(a, b):
                    return a + b


                class Greeter:
                    def __init__(self, name):
                        self.name = name

                    def hello(self):
                        return f"Hi {self.name}"


                def standalone():
                    pass
                """).lstrip()
        )

    def test_emits_function_and_class(self, big_python_source):
        chunks = chunk_source("python", "demo.py", big_python_source)
        kinds = [c.entity_kind for c in chunks]
        # Class definition is emitted; the methods inside are emitted too.
        assert "function_definition" in kinds
        assert "class_definition" in kinds

    def test_class_methods_become_their_own_chunks(self, big_python_source):
        chunks = chunk_source("python", "demo.py", big_python_source)
        names = {c.entity_name for c in chunks if c.entity_name}
        # add() and standalone() are top-level functions; hello() and
        # __init__ are inside Greeter — all four should appear by name.
        assert "add" in names
        assert "standalone" in names
        assert "hello" in names
        assert "__init__" in names
        assert "Greeter" in names

    def test_line_ranges_are_1_indexed_and_inclusive(self, big_python_source):
        chunks = chunk_source("python", "demo.py", big_python_source)
        for c in chunks:
            assert c.line_start >= 1
            assert c.line_end >= c.line_start

    def test_hash_stable_across_repeated_calls(self, big_python_source):
        a = chunk_source("python", "demo.py", big_python_source)
        b = chunk_source("python", "demo.py", big_python_source)
        assert [c.stable_hash for c in a] == [c.stable_hash for c in b]


# ── TypeScript chunking ──────────────────────────────────────────────────

class TestTypescriptChunker:
    @pytest.fixture
    def big_ts_source(self):
        return (
            "// header\n" * 45 +
            textwrap.dedent("""
                export function add(a: number, b: number): number {
                    return a + b;
                }

                export class Greeter {
                    constructor(public name: string) {}
                    hello(): string {
                        return `Hi ${this.name}`;
                    }
                }

                export interface User {
                    id: string;
                    email: string;
                }
                """).lstrip()
        )

    def test_emits_function_class_interface(self, big_ts_source):
        chunks = chunk_source("typescript", "demo.ts", big_ts_source)
        kinds = {c.entity_kind for c in chunks}
        assert "function_declaration" in kinds
        assert "class_declaration" in kinds
        assert "interface_declaration" in kinds

    def test_function_name_extracted(self, big_ts_source):
        chunks = chunk_source("typescript", "demo.ts", big_ts_source)
        names = {c.entity_name for c in chunks if c.entity_name}
        assert "add" in names
        assert "Greeter" in names
        assert "User" in names


# ── Unparseable / unsupported fallback ───────────────────────────────────

class TestFallback:
    def test_unsupported_language_skips(self):
        # detect_language returns None -> chunk_source for unknown lang
        # falls through to file fallback.
        chunks = chunk_source("rust", "a.rs", "fn main() {}")
        assert len(chunks) == 1
        assert chunks[0].entity_kind == "file"

    def test_big_unparseable_file_windowed(self):
        # 200 lines of garbage that detect_language can't help with;
        # chunker emits FALLBACK_WINDOW_LINES-sized windows.
        garbage = "\n".join(f"line {i}" for i in range(200))
        chunks = chunk_source("unknown", "junk.txt", garbage)
        assert len(chunks) >= 2
        assert all(c.entity_kind in ("file_window", "file") for c in chunks)


# ── chunk_file end-to-end ────────────────────────────────────────────────

class TestChunkFile:
    def test_reads_file_and_chunks(self, tmp_path):
        f = tmp_path / "demo.py"
        f.write_text("def hello():\n    return 42\n")
        chunks = chunk_file(f)
        assert len(chunks) >= 1
        assert chunks[0].file_path == str(f)

    def test_returns_empty_on_unsupported_ext(self, tmp_path):
        f = tmp_path / "demo.unknown"
        f.write_text("anything")
        assert chunk_file(f) == []
