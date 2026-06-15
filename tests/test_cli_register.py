"""CLI tests for ``memstrata register`` — V5.2-E auto-ingestion reactivation.

Pre-V5.2-E this command was a stub that printed "Registered" without
touching the DB, so the cd-hook fired against /dev/null. These tests
guard the post-V5.2-E wiring: the CLI must actually write a
``project_opt_in`` row so a daemon (or a subsequent ``memstrata api``
restart) can pick the project up.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

import pytest

from memstrata.cli.main import _cmd_register
from memstrata.layer3._db import get_db_path, init_db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ML_DB_PATH", str(tmp_path / "core.db"))


def _git_project(tmp_path):
    project = tmp_path / "demo-proj"
    project.mkdir()
    (project / ".git").mkdir()  # CLI requires .git/ to proceed
    return project


def test_register_writes_opt_in_row(tmp_path):
    project = _git_project(tmp_path)
    args = argparse.Namespace(path=str(project), quiet=False)

    _cmd_register(args)

    conn = sqlite3.connect(str(get_db_path()))
    try:
        init_db(conn)
        row = conn.execute(
            "SELECT state FROM project_opt_in WHERE project_path = ?",
            (str(project.resolve()),),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "opted_in"


def test_register_quiet_still_writes(tmp_path, capsys):
    project = _git_project(tmp_path)
    args = argparse.Namespace(path=str(project), quiet=True)

    _cmd_register(args)

    captured = capsys.readouterr()
    assert captured.out == ""

    conn = sqlite3.connect(str(get_db_path()))
    try:
        init_db(conn)
        row = conn.execute(
            "SELECT state FROM project_opt_in WHERE project_path = ?",
            (str(project.resolve()),),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None


def test_register_skips_non_git_directory(tmp_path, capsys):
    project = tmp_path / "plain-dir"
    project.mkdir()
    args = argparse.Namespace(path=str(project), quiet=False)

    _cmd_register(args)

    out = capsys.readouterr().out
    assert "not a git repository" in out

    conn = sqlite3.connect(str(get_db_path()))
    try:
        init_db(conn)
        row = conn.execute(
            "SELECT COUNT(*) FROM project_opt_in WHERE project_path = ?",
            (str(project.resolve()),),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == 0


def test_register_exits_nonzero_on_missing_path(tmp_path):
    ghost = tmp_path / "does-not-exist"
    args = argparse.Namespace(path=str(ghost), quiet=False)

    with pytest.raises(SystemExit) as exc_info:
        _cmd_register(args)
    assert exc_info.value.code == 1
