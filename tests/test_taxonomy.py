"""Tests for taxonomy loading and `categories` table seeding."""
from __future__ import annotations

import sqlite3

from acp_decisions.taxonomy import load_taxonomy, seed_categories


def test_load_taxonomy_returns_categories() -> None:
    cats = load_taxonomy()
    assert len(cats) >= 20
    ids = [c["id"] for c in cats]
    assert "zoning_contravention" in ids
    assert "natura_appropriate_assessment" in ids
    assert "other" in ids
    assert len(set(ids)) == len(ids), "category IDs must be unique"


def test_load_taxonomy_categories_have_required_fields() -> None:
    cats = load_taxonomy()
    for c in cats:
        assert {"id", "name", "group", "description"} <= set(c.keys())
        assert c["id"] and c["name"] and c["group"] and c["description"]


def test_seed_categories_inserts_rows(temp_db: sqlite3.Connection) -> None:
    seed_categories(temp_db)
    n = temp_db.execute("SELECT COUNT(*) AS n FROM categories").fetchone()["n"]
    cats = load_taxonomy()
    assert n == len(cats)


def test_seed_categories_is_idempotent(temp_db: sqlite3.Connection) -> None:
    """Running seed_categories twice should not duplicate rows."""
    seed_categories(temp_db)
    seed_categories(temp_db)
    n = temp_db.execute("SELECT COUNT(*) AS n FROM categories").fetchone()["n"]
    cats = load_taxonomy()
    assert n == len(cats)


def test_seed_categories_persists_group_label(temp_db: sqlite3.Connection) -> None:
    seed_categories(temp_db)
    row = temp_db.execute(
        "SELECT group_label FROM categories WHERE id = 'zoning_contravention'"
    ).fetchone()
    assert row["group_label"] == "planning_policy"
