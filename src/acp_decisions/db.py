"""SQLite database layer for the ACP decisions archive."""
from __future__ import annotations

import sqlite3
from pathlib import Path

# Path to the shipped schema.sql, resolved relative to this module
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def open_db(path: Path | str) -> sqlite3.Connection:
    """Open the database at `path`, applying the schema if needed.

    Returns a connection with foreign keys enabled and Row factory set
    so callers can use column names rather than indices.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn
