"""Shared pytest fixtures for the acp_decisions test suite."""
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def temp_db(tmp_path: Path) -> sqlite3.Connection:
    """A fresh SQLite connection backed by a tempfile, schema applied."""
    from acp_decisions.db import open_db

    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
