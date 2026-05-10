"""Assign taxonomy categories to council_refusal_reasons.

We already have plain-English summaries for every council reason (from a prior
classifier pass). This script takes that summary (single sentence) and asks
Gemma 4 to pick 1-3 category IDs from our fixed taxonomy. Output goes into
the council_reason_categories join table.

Why summary instead of raw_text? Faster — the prompt is much shorter, model
converges faster (~1-2 sec/reason vs 5 sec). The summary already encodes the
key reasoning; we just need a category label on top.

Idempotent — picks rows where the reason has no entry in council_reason_categories yet.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

from acp_decisions.classifier import OllamaClient
from acp_decisions.db import open_db
from acp_decisions.taxonomy import load_taxonomy


def build_category_only_prompt(reason_summary: str, categories) -> str:
    """A trimmed prompt — categories list + summary text, asks for 1-3 IDs only."""
    cat_lines = "\n".join(
        f"- {c['id']} — {c['name']}" for c in categories
    )
    return (
        "Pick 1 to 3 category IDs that best describe this Irish planning "
        "refusal reason.\n\n"
        f"Reason: {reason_summary.strip()}\n\n"
        f"Categories:\n{cat_lines}\n\n"
        'Respond with ONLY: {"category_ids": ["id1", "id2"]}'
    )


def classify_categories(client: OllamaClient, summary: str, categories) -> list[str]:
    """Return validated category IDs for a summary. Falls back to ['other']."""
    valid_ids = {c["id"] for c in categories}
    try:
        raw = client.generate(build_category_only_prompt(summary, categories))
        parsed = json.loads(raw)
    except (json.JSONDecodeError, httpx.HTTPError):
        return ["other"]
    ids = parsed.get("category_ids") if isinstance(parsed, dict) else []
    if not isinstance(ids, list):
        return ["other"]
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        if isinstance(i, str) and i in valid_ids and i not in seen:
            seen.add(i)
            out.append(i)
    return out or ["other"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="acp.db")
    parser.add_argument("--model", default="gemma4:e2b")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    conn = open_db(args.db)
    conn.row_factory = __import__("sqlite3").Row

    sql = """
        SELECT id, summary
          FROM council_refusal_reasons
         WHERE summary IS NOT NULL AND summary != ''
           AND id NOT IN (SELECT reason_id FROM council_reason_categories)
         ORDER BY id
        """
    if args.limit > 0:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql).fetchall()
    print(f"to categorise: {len(rows):,} council reasons", flush=True)

    categories = load_taxonomy()
    client = OllamaClient(model=args.model)
    t0 = time.monotonic()
    n_done = 0
    try:
        for i, r in enumerate(rows, 1):
            cat_ids = classify_categories(client, r["summary"], categories)
            for cid in cat_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO council_reason_categories (reason_id, category_id) VALUES (?, ?)",
                    (r["id"], cid),
                )
            n_done += 1
            if i % 200 == 0:
                conn.commit()
                rate = i / (time.monotonic() - t0)
                eta_min = (len(rows) - i) / rate / 60
                print(f"  {i:,}/{len(rows):,}  ~{rate:.1f}/s ETA {eta_min:.0f} min", flush=True)
    finally:
        conn.commit()
        client.close()

    print(f"done. categorised {n_done:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
