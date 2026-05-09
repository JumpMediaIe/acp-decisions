"""Run the same entity-extraction classifier on council_refusal_reasons.

Identical to the existing reason classifier but writes to the council_refusal_reasons
table instead of refusal_reasons. Picks only rows where summary IS NULL so it's
restartable / incremental.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone

from acp_decisions.classifier import OllamaClient, analyse_reason
from acp_decisions.taxonomy import load_taxonomy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="acp.db")
    parser.add_argument("--model", default="gemma4:e2b")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    sql = (
        "SELECT id, raw_text FROM council_refusal_reasons "
        "WHERE summary IS NULL AND raw_text IS NOT NULL AND raw_text != '' "
        "ORDER BY id"
    )
    if args.limit > 0:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql).fetchall()
    print(f"to classify: {len(rows):,} council reasons", flush=True)

    categories = load_taxonomy()
    client = OllamaClient(model=args.model)
    t0 = time.monotonic()
    n = 0
    try:
        for i, r in enumerate(rows, 1):
            analysis = analyse_reason(client, r["raw_text"], categories)
            conn.execute(
                """
                UPDATE council_refusal_reasons
                   SET summary                = ?,
                       dev_plan               = ?,
                       policy_codes           = ?,
                       quantitative_violation = ?,
                       statutory_test         = ?,
                       classified_at          = ?
                 WHERE id = ?
                """,
                (
                    analysis.summary,
                    analysis.dev_plan,
                    json.dumps(analysis.policy_codes) if analysis.policy_codes else None,
                    analysis.quantitative_violation,
                    analysis.statutory_test,
                    datetime.now(timezone.utc).isoformat(),
                    r["id"],
                ),
            )
            n += 1
            if i % 100 == 0:
                conn.commit()
                rate = i / (time.monotonic() - t0)
                eta_min = (len(rows) - i) / rate / 60
                print(f"  {i:,}/{len(rows):,}  ~{rate:.1f}/s ETA {eta_min:.0f} min", flush=True)
    finally:
        conn.commit()
        client.close()

    print(f"done. classified {n:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
