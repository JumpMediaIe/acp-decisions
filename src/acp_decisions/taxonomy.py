"""Load the refusal-reason taxonomy from YAML and seed the `categories` table.

The taxonomy lives in `taxonomy.yaml` next to this module; it's the single
source of truth for category IDs the LLM classifier maps each reason into.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TypedDict

import yaml


_TAXONOMY_PATH = Path(__file__).parent / "taxonomy.yaml"


class Category(TypedDict):
    id: str
    name: str
    group: str
    description: str
    example: str


def load_taxonomy() -> list[Category]:
    """Parse the YAML file and return the list of categories."""
    raw = yaml.safe_load(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    cats = raw["categories"]
    out: list[Category] = []
    for c in cats:
        out.append(
            Category(
                id=c["id"].strip(),
                name=c["name"].strip(),
                group=c["group"].strip(),
                description=c["description"].strip(),
                example=(c.get("example") or "").strip(),
            )
        )
    return out


def seed_categories(conn: sqlite3.Connection) -> None:
    """Insert (or replace) every taxonomy category into the categories table.

    Idempotent: running this on each connection on startup keeps the DB in sync
    with the YAML if the editor adds a new category.
    """
    cats = load_taxonomy()
    conn.executemany(
        "INSERT INTO categories (id, name, description, group_label, example) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "  name = excluded.name, "
        "  description = excluded.description, "
        "  group_label = excluded.group_label, "
        "  example = excluded.example",
        [(c["id"], c["name"], c["description"], c["group"], c["example"]) for c in cats],
    )
    conn.commit()
