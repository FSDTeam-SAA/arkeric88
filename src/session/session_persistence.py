"""
Durable storage for city and activity sessions.

Sessions used to live only in process memory, so every restart or redeploy
lost them: a saved search in the guest's Search History then pointed at a
session_id the API no longer knew, and opening it (the eye icon) failed.
Sessions are now written through to a small SQLite file and read back on a
cache miss. Set SESSION_DB_PATH to choose the file, or to an empty string to
keep sessions in memory only.
"""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, List, Optional

DEFAULT_DB_PATH = os.path.join("data", "sessions.sqlite3")
_LOCK = threading.Lock()


def db_path() -> Optional[str]:
    path = os.getenv("SESSION_DB_PATH", DEFAULT_DB_PATH)
    return path or None


@contextmanager
def _connect(path: str) -> Iterator[sqlite3.Connection]:
    """One short-lived connection per operation: committed on success, always closed."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with _LOCK:
        connection = sqlite3.connect(path)
        try:
            # Write-ahead log with normal sync: durable across app restarts,
            # without a full disk flush on every session write.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                " kind TEXT NOT NULL, id TEXT NOT NULL, parent_id TEXT, payload TEXT NOT NULL,"
                " PRIMARY KEY (kind, id))"
            )
            yield connection
            connection.commit()
        finally:
            connection.close()


def save(kind: str, session_id: str, payload: dict, parent_id: Optional[str] = None) -> None:
    path = db_path()
    if not path:
        return
    with _connect(path) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO sessions (kind, id, parent_id, payload) VALUES (?, ?, ?, ?)",
            (kind, session_id, parent_id, json.dumps(payload, default=str)),
        )


def load(kind: str, session_id: str) -> Optional[dict]:
    path = db_path()
    if not path or not os.path.exists(path):
        return None
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT payload FROM sessions WHERE kind = ? AND id = ?", (kind, session_id)
        ).fetchone()
    return json.loads(row[0]) if row else None


def load_children(kind: str, parent_id: str) -> List[dict]:
    path = db_path()
    if not path or not os.path.exists(path):
        return []
    with _connect(path) as connection:
        rows = connection.execute(
            "SELECT payload FROM sessions WHERE kind = ? AND parent_id = ?", (kind, parent_id)
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def delete(kind: str, session_id: str) -> bool:
    path = db_path()
    if not path or not os.path.exists(path):
        return False
    with _connect(path) as connection:
        cursor = connection.execute("DELETE FROM sessions WHERE kind = ? AND id = ?", (kind, session_id))
    return cursor.rowcount > 0
