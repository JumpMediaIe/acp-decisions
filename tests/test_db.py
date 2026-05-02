"""Tests for the database layer."""
from pathlib import Path

from acp_decisions.db import open_db


def test_open_db_creates_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)

    # Query SQLite catalogue
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cur.fetchall()]
    conn.close()

    expected = {
        "decisions",
        "refusal_reasons",
        "reason_categories",
        "categories",
        "scrape_errors",
    }
    assert expected.issubset(set(tables))


def test_open_db_creates_fts_table(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='decisions_fts'"
    )
    assert cur.fetchone() is not None
    conn.close()


def test_open_db_is_idempotent(tmp_path: Path) -> None:
    """Calling open_db twice on the same path doesn't error."""
    db_path = tmp_path / "test.db"
    conn1 = open_db(db_path)
    conn1.close()
    conn2 = open_db(db_path)  # would raise if CREATE TABLE missing IF NOT EXISTS
    conn2.close()
