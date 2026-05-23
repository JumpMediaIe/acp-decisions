"""Backfill full development descriptions from the agile portal.

LGMA's national dataset truncates DevelopmentDescription to ~80 characters
for some councils (Dun Laoghaire Rathdown, Fingal). That breaks the
development-type mapper because the text is just boilerplate ("Permission
for development. The development will consist of...") with no actual
content.

The agile portal's /api/application/{id} endpoint returns a fullProposal
field with the complete text. For every council application we've already
fetched reason text for (i.e. exists in council_reasons_fetch with a
known reference + slug), this script:

    1. Re-searches the agile API to get the application's internal id
    2. Hits /api/application/{id} to get fullProposal
    3. Replaces development_description in planning_applications
    4. Recomputes development_type_id via map_devtype()

Safe to re-run; uses an existing council_reasons_fetch row as the
trigger so unfetched applications aren't touched.
"""
from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import urlparse

import httpx

from acp_decisions.agile_api import AgileApiClient, AgileApiError
from acp_decisions.db import open_db
from acp_decisions.devtype_map import map_devtype


def _slug_from_link(link: str | None) -> str | None:
    if not link:
        return None
    import re
    m = re.search(r"agileapplications\.ie/([a-z0-9-]+)", link, re.I)
    return m.group(1).lower() if m else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="acp.db")
    parser.add_argument("--delay", type=float, default=0.7)
    parser.add_argument("--authority", type=str, default=None,
                        help="Limit to a specific planning_authority (e.g. 'Dun Laoghaire Rathdown County Council')")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    conn = open_db(args.db)

    where_authority = ""
    params: list = []
    if args.authority:
        where_authority = " AND pa.planning_authority = ?"
        params.append(args.authority)

    sql = (
        "SELECT pa.object_id, pa.application_number, pa.link_app_details, "
        "       pa.development_description "
        "  FROM planning_applications pa "
        "  JOIN council_reasons_fetch f ON f.object_id = pa.object_id "
        " WHERE pa.link_app_details LIKE '%agileapplications%' "
        f"   {where_authority} "
        " ORDER BY pa.object_id"
    )
    if args.limit > 0:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql, params).fetchall()
    print(f"to backfill: {len(rows):,} applications", flush=True)
    if not rows:
        return 0

    api = AgileApiClient()
    n_updated = 0
    n_skipped = 0
    n_error = 0
    t0 = time.monotonic()

    try:
        for i, r in enumerate(rows, 1):
            slug = _slug_from_link(r["link_app_details"])
            ref = r["application_number"]
            if not slug or not ref:
                n_skipped += 1
                continue

            try:
                app_id = api.search_application_id(slug, ref)
                if app_id is None:
                    n_skipped += 1
                    continue
                time.sleep(args.delay)
                resp = api._client.get(
                    f"{api._get_tenant_api(slug)}/api/application/{app_id}",
                    headers=api._tenant_headers(slug),
                )
                if resp.status_code != 200:
                    n_error += 1
                    continue
                data = resp.json()
                full = (data.get("fullProposal") or "").strip()
                if not full or len(full) <= len(r["development_description"] or ""):
                    n_skipped += 1
                    continue
                dev_type = map_devtype(full)
                conn.execute(
                    "UPDATE planning_applications SET development_description = ?, "
                    "       development_type_id = ? WHERE object_id = ?",
                    (full, dev_type, r["object_id"]),
                )
                n_updated += 1
            except (AgileApiError, httpx.HTTPError) as e:
                n_error += 1
                print(f"  err {ref}: {e}", flush=True)

            time.sleep(args.delay)

            if i % 50 == 0:
                conn.commit()
                rate = i / (time.monotonic() - t0)
                eta_min = (len(rows) - i) / rate / 60
                print(
                    f"  {i:,}/{len(rows):,}  updated={n_updated:,} "
                    f"skipped={n_skipped:,} errors={n_error:,}  "
                    f"~{rate:.2f}/s ETA {eta_min:.0f} min",
                    flush=True,
                )
    finally:
        conn.commit()
        api.close()

    print(
        f"done. updated={n_updated:,} skipped={n_skipped:,} errors={n_error:,}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
