"""Fork-local regression guard: the library index must not leak sqlite handles.

``with sqlite3.connect(...) as conn`` ends the transaction but does NOT close the
connection. That left ``.last30days-library.db`` open after every run — on Windows
the caller's TemporaryDirectory cleanup then died with WinError 32, and on POSIX it
was a silent fd leak. Four tests in test_cli_v3.py were skipped on Windows because
of it. These tests fail if the pattern comes back (including via an upstream sync).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/last30days/scripts"))

from lib import library_index  # noqa: E402


@pytest.fixture
def connection_spy(monkeypatch):
    """Record every sqlite3 connection library_index opens."""
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(library_index.sqlite3, "connect", spy)
    return opened


def _is_closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def test_sync_library_closes_every_connection(tmp_path, connection_spy):
    briefs = tmp_path / "briefings"
    briefs.mkdir()
    db_path = tmp_path / "library.db"

    library_index.sync_library(tmp_path, briefs, db_path=db_path)

    assert connection_spy, "expected sync_library to open at least one connection"
    assert all(_is_closed(conn) for conn in connection_spy)


def test_search_closes_every_connection(tmp_path, connection_spy):
    briefs = tmp_path / "briefings"
    briefs.mkdir()
    db_path = tmp_path / "library.db"
    library_index.sync_library(tmp_path, briefs, db_path=db_path)
    connection_spy.clear()

    library_index.search("anything", db_path=db_path, store_db_path=tmp_path / "research.db")

    assert all(_is_closed(conn) for conn in connection_spy)


def test_index_db_is_deletable_after_use(tmp_path):
    """The Windows symptom, asserted directly: no lingering handle on the file."""
    briefs = tmp_path / "briefings"
    briefs.mkdir()
    db_path = tmp_path / "library.db"

    library_index.sync_library(tmp_path, briefs, db_path=db_path)
    library_index.search("anything", db_path=db_path, store_db_path=tmp_path / "research.db")

    db_path.unlink()  # raises PermissionError (WinError 32) if a handle is still open
    assert not db_path.exists()
