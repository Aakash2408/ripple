"""
ripple/app/poll_store.py

SQLite-backed persistence for the polling endpoint.

Tracks the last-checked commit SHA per repository so /poll can detect which
repos have new commits since the last check. Survives container restarts when
stored on a Railway Volume or persistent disk.

WHY SQLITE AND NOT JSON
The token_store and activity log use JSON because they are append-only or
small. The poll store needs atomic per-row updates under concurrent requests
(two /poll calls arriving simultaneously must not clobber each other's SHA),
and SQLite gives that for free without an external database.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


# Reuse the same directory search pattern as token_store.py.
_DATA_DIR_CANDIDATES = [
    os.environ.get("RIPPLE_DATA_DIR", ""),
    "/app/data",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"),
    "/tmp/ripple-data",
]


def _find_db_path() -> Path:
    for candidate in _DATA_DIR_CANDIDATES:
        if not candidate:
            continue
        p = Path(candidate)
        try:
            p.mkdir(parents=True, exist_ok=True)
            test = p / ".write_test_poll"
            test.write_text("ok")
            test.unlink()
            return p / "poll_state.db"
        except (IOError, OSError):
            continue
    return Path("/tmp/ripple-data/poll_state.db")


_DB_PATH = _find_db_path()
_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS poll_state (
    repo        TEXT PRIMARY KEY,
    last_sha    TEXT NOT NULL,
    last_polled TEXT NOT NULL,
    last_result TEXT DEFAULT ''
);
"""


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
    return conn


def get_last_sha(repo: str) -> str:
    """Last-checked SHA for a repo, or '' if never polled."""
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT last_sha FROM poll_state WHERE repo = ?", (repo,)
            ).fetchone()
            return row[0] if row else ""
        finally:
            conn.close()


def set_last_sha(repo: str, sha: str, result: str = ""):
    """Update the last-checked SHA after a successful poll."""
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO poll_state (repo, last_sha, last_polled, last_result)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(repo) DO UPDATE SET
                       last_sha = excluded.last_sha,
                       last_polled = excluded.last_polled,
                       last_result = excluded.last_result""",
                (repo, sha, now, result),
            )
            conn.commit()
        finally:
            conn.close()


def get_all_watched() -> list[dict]:
    """All repos that have ever been polled, with their last state."""
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT repo, last_sha, last_polled, last_result FROM poll_state "
                "ORDER BY last_polled DESC"
            ).fetchall()
            return [
                {"repo": r[0], "last_sha": r[1], "last_polled": r[2],
                 "last_result": r[3]}
                for r in rows
            ]
        finally:
            conn.close()


def remove_repo(repo: str) -> bool:
    """Stop watching a repo. Returns True if it existed."""
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM poll_state WHERE repo = ?", (repo,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def clear_all():
    """Remove all poll state. Used in tests."""
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("DELETE FROM poll_state")
            conn.commit()
        finally:
            conn.close()
