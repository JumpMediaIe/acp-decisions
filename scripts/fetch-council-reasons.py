"""Fetch refusal-reason text for council applications via the agileapplications.ie API.

Iterates over every refused planning_applications row whose link_app_details
points at agileapplications.ie. For each one:

    1. Look up the application's internal ID via the search endpoint
    2. Fetch /conditions and filter prescriptionCode='R' (refusal reasons)
    3. Insert into council_refusal_reasons (replace previous on re-run)
    4. Record success / empty / error in council_reasons_fetch so we can
       resume incrementally on a re-run

Polite delay between API calls; safe to ctrl-C and resume — the
council_reasons_fetch table tracks what we've already attempted.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

import httpx

from acp_decisions.agile_api import AgileApiClient, AgileApiError
from acp_decisions.db import open_db


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="acp.db")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds between API calls (default 1s, per polite-scraping defaults).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after N applications (0 = all). Useful for trial runs.")
    parser.add_argument("--retry", action="store_true",
                        help="Also retry rows in council_reasons_fetch with non-empty error_message.")
    parser.add_argument("--from-date", type=str, default="2011-01-01",
                        help="Only fetch refusals decided on or after this date "
                             "(default 2011-01-01; pre-2010 council data is not migrated to the API).")
    args = parser.parse_args()

    # open_db applies schema.sql so the new council_refusal_reasons /
    # council_reasons_fetch tables exist on first run.
    conn = open_db(args.db)

    # Build the work list: refused agile applications NOT yet in council_reasons_fetch
    # (or with errors if --retry).
    where_extra = ""
    if not args.retry:
        # Skip everything previously attempted, success or error
        where_extra = (
            " AND NOT EXISTS ("
            " SELECT 1 FROM council_reasons_fetch f WHERE f.object_id = pa.object_id"
            ")"
        )
    else:
        # Retry rows with errors only; skip successful past fetches
        where_extra = (
            " AND NOT EXISTS ("
            " SELECT 1 FROM council_reasons_fetch f"
            " WHERE f.object_id = pa.object_id AND (f.error_message IS NULL OR f.error_message = '')"
            ")"
        )
    sql = (
        "SELECT pa.object_id, pa.application_number, pa.link_app_details "
        "  FROM planning_applications pa "
        " WHERE pa.decision LIKE '%Refuse%' "
        "   AND pa.link_app_details LIKE '%agileapplications%' "
        "   AND pa.application_number IS NOT NULL "
        "   AND pa.application_number != '' "
        "   AND pa.decision_date >= ? "
        f"  {where_extra}"
        " ORDER BY pa.object_id"
    )
    if args.limit > 0:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql, (args.from_date,)).fetchall()
    print(f"to fetch: {len(rows):,} council applications", flush=True)

    api = AgileApiClient()
    n_with_reasons = 0
    n_empty = 0
    n_error = 0
    t0 = time.monotonic()
    try:
        for i, r in enumerate(rows, 1):
            object_id = r["object_id"]
            slug = api.slug_from_link(r["link_app_details"])
            ref = r["application_number"]
            error_msg: str | None = None
            reasons = []
            try:
                if not slug:
                    raise AgileApiError("could not extract slug from link")
                app_id = api.search_application_id(slug, ref)
                if app_id is None:
                    raise AgileApiError("not found via search")
                time.sleep(args.delay)
                reasons = api.fetch_refusal_reasons(slug, app_id)
            except (AgileApiError, httpx.HTTPError) as e:
                error_msg = str(e)[:300]

            now = datetime.now(timezone.utc).isoformat()
            # Replace previous reasons for this case (idempotent)
            conn.execute("DELETE FROM council_refusal_reasons WHERE object_id = ?", (object_id,))
            for reason in reasons:
                conn.execute(
                    """
                    INSERT INTO council_refusal_reasons
                        (object_id, reason_number, short_prescription, raw_text, fetched_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (object_id, reason.reason_number, reason.short_prescription,
                     reason.long_prescription, now),
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO council_reasons_fetch
                    (object_id, fetched_at, reasons_count, error_message)
                VALUES (?, ?, ?, ?)
                """,
                (object_id, now, len(reasons), error_msg),
            )

            if error_msg:
                n_error += 1
            elif reasons:
                n_with_reasons += 1
            else:
                n_empty += 1

            time.sleep(args.delay)

            if i % 50 == 0:
                conn.commit()
                rate = i / (time.monotonic() - t0)
                eta_min = (len(rows) - i) / rate / 60
                print(
                    f"  {i:,}/{len(rows):,}  "
                    f"with_reasons={n_with_reasons:,} empty={n_empty:,} errors={n_error:,}  "
                    f"~{rate:.2f}/s ETA {eta_min:.0f} min",
                    flush=True,
                )
    finally:
        conn.commit()
        api.close()

    print(f"done. with_reasons={n_with_reasons:,} empty={n_empty:,} errors={n_error:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
