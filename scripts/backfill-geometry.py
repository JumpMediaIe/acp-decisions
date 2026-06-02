"""Backfill lat/lng for refused planning_applications from the LGMA layer geometry.

The LGMA dataset's ITMEasting/ITMNorthing attribute fields are empty, but the
ArcGIS layer DOES carry a point geometry per application. Requesting it with
outSR=4326 returns clean WGS84 lng/lat. We match rows on the natural key
(planning_authority, application_number) — the same key lgma.py upserts on —
and write lat/lng. Idempotent; safe to re-run (only fills rows still missing
coords unless --all).

Usage:
    uv run python scripts/backfill-geometry.py --db acp.db [--all]
"""
from __future__ import annotations

import argparse
import time

import httpx

from acp_decisions.db import open_db

FS = (
    "https://services.arcgis.com/NzlPQPKn5QF9v2US/"
    "arcgis/rest/services/IrishPlanningApplications/FeatureServer/0/query"
)
PAGE = 2000
WHERE = "Decision LIKE '%Refuse%'"


def _ensure_columns(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(planning_applications)")}
    if "lat" not in cols:
        conn.execute("ALTER TABLE planning_applications ADD COLUMN lat REAL")
    if "lng" not in cols:
        conn.execute("ALTER TABLE planning_applications ADD COLUMN lng REAL")
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="acp.db")
    ap.add_argument("--all", action="store_true",
                    help="Re-fetch coords for every refusal (default: only those missing).")
    args = ap.parse_args()

    conn = open_db(args.db)
    _ensure_columns(conn)

    # Local lookup of rows that still need coords, keyed by (authority, ref).
    need: dict[tuple[str, str], int] = {}
    sql = ("SELECT object_id, planning_authority, application_number "
           "FROM planning_applications WHERE UPPER(decision) LIKE '%REFUSE%'")
    if not args.all:
        sql += " AND (lat IS NULL OR lng IS NULL)"
    for oid, auth, ref in conn.execute(sql):
        need[(auth or "", ref or "")] = oid
    print(f"rows needing coords: {len(need):,}", flush=True)
    if not need:
        return 0

    matched = 0
    offset = 0
    t0 = time.monotonic()
    with httpx.Client(timeout=90, headers={"User-Agent": "planningcheck.ie geometry backfill"}) as c:
        while True:
            r = c.get(FS, params={
                "where": WHERE,
                "outFields": "PlanningAuthority,ApplicationNumber",
                "returnGeometry": "true",
                "outSR": "4326",
                "resultOffset": str(offset),
                "resultRecordCount": str(PAGE),
                "orderByFields": "OBJECTID",
                "f": "json",
            })
            r.raise_for_status()
            data = r.json()
            feats = data.get("features", [])
            if not feats:
                break
            batch = []
            for f in feats:
                a = f.get("attributes", {})
                g = f.get("geometry") or {}
                lng, lat = g.get("x"), g.get("y")
                if lat is None or lng is None:
                    continue
                oid = need.get(((a.get("PlanningAuthority") or ""), (a.get("ApplicationNumber") or "")))
                if oid is not None:
                    batch.append((lat, lng, oid))
            if batch:
                conn.executemany(
                    "UPDATE planning_applications SET lat=?, lng=? WHERE object_id=?", batch)
                conn.commit()
                matched += len(batch)
            offset += len(feats)
            rate = offset / (time.monotonic() - t0)
            print(f"  scanned {offset:,} source rows, matched {matched:,}  ~{rate:.0f}/s", flush=True)
            if not data.get("exceededTransferLimit"):
                break

    print(f"done. matched {matched:,} of {len(need):,} needing coords")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
