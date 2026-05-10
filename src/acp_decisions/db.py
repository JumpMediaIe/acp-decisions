"""SQLite database layer for the ACP decisions archive."""
from __future__ import annotations

import sqlite3
from pathlib import Path

# Path to the shipped schema.sql, resolved relative to this module
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Columns that may need to be added to existing tables when the schema evolves.
# SQLite has no `ALTER TABLE ADD COLUMN IF NOT EXISTS`, so we apply each one
# only when the column is missing. Adding a column to an existing row is a
# zero-cost operation in SQLite (column gets NULL for existing rows).
_PENDING_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "refusal_reasons": [
        ("summary", "TEXT"),
        ("dev_plan", "TEXT"),
        ("policy_codes", "TEXT"),
        ("quantitative_violation", "TEXT"),
        ("statutory_test", "TEXT"),
    ],
    "planning_applications": [
        # Mapped DevelopmentTypeId derived from `development_description`
        # via devtype_map.py. Backfilled on lgma-sync.
        ("development_type_id", "TEXT"),
    ],
    "categories": [
        # Plain-language example, displayed alongside name + description in UI.
        ("example", "TEXT"),
    ],
}


def open_db(path: Path | str) -> sqlite3.Connection:
    """Open the database at `path`, applying the schema and any column-adds.

    Returns a connection with foreign keys enabled and Row factory set
    so callers can use column names rather than indices.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    _apply_column_migrations(conn)
    conn.commit()
    return conn


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    """Add any columns from _PENDING_COLUMNS that don't yet exist on each table."""
    for table, cols in _PENDING_COLUMNS.items():
        existing = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, ddl in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
