"""Produce a slimmed copy of acp.db suitable for shipping with the website.

Strips columns, tables and indexes that the planningcheck.ie Next.js app
never queries, then VACUUMs. Brings the file size from ~130 MB to ~93 MB
so it fits under GitHub's per-file limit without LFS.

Usage:
    uv run python scripts/slim-shipped-db.py --src acp.db --dst data/acp.db

The source DB is left untouched. The destination is a fresh copy with:
    - planning_applications: only columns referenced by the website
    - council_refusal_reasons: minus fetched_at/classified_at
    - documents: minus fetched_at
    - operational tables dropped (council_reasons_fetch, scrape_errors, FTS5)
    - non-refused planning_applications rows have description/address/link
      nulled (they exist only to make the 'appealed to ACP' count add up;
      the website never displays their text payload)
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path


# Columns of planning_applications that the website actually queries.
# Verified against irish-planning-tool/lib/decisions/councilQueries.ts and
# usage across the wider app — no other column is read at runtime.
KEEP_PA_COLUMNS = [
    "object_id",
    "planning_authority",
    "application_number",
    "development_description",
    "development_address",
    "decision",
    "decision_date",
    "one_off_house",
    "development_type_id",
    "link_app_details",
    "appeal_ref_number",
    "appeal_decision",
    "lat",
    "lng",
]


KEEP_CRR_COLUMNS = [
    "id",
    "object_id",
    "reason_number",
    "short_prescription",
    "raw_text",
    "summary",
    "dev_plan",
    "policy_codes",
    "quantitative_violation",
    "statutory_test",
]


# Operational / staging tables the website never reads.
OPERATIONAL_TABLES = [
    "council_reasons_fetch",
    "scrape_errors",
    "decisions_fts",
    "decisions_fts_config",
    "decisions_fts_data",
    "decisions_fts_docsize",
    "decisions_fts_idx",
]


def slim(dst: Path) -> None:
    """Apply all slimming steps to the DB at `dst` in place."""
    conn = sqlite3.connect(dst)
    conn.execute("PRAGMA foreign_keys = OFF")

    # planning_applications: rebuild with only the kept columns
    cols = ", ".join(KEEP_PA_COLUMNS)
    conn.executescript(
        f"""
        BEGIN;
        CREATE TABLE planning_applications_new (
            object_id INTEGER PRIMARY KEY,
            planning_authority TEXT,
            application_number TEXT,
            development_description TEXT,
            development_address TEXT,
            decision TEXT,
            decision_date TEXT,
            one_off_house TEXT,
            development_type_id TEXT,
            link_app_details TEXT,
            appeal_ref_number TEXT,
            appeal_decision TEXT,
            lat REAL,
            lng REAL
        );
        INSERT INTO planning_applications_new ({cols})
            SELECT {cols} FROM planning_applications;
        DROP TABLE planning_applications;
        ALTER TABLE planning_applications_new RENAME TO planning_applications;
        CREATE INDEX idx_pa_authority ON planning_applications(planning_authority);
        CREATE INDEX idx_pa_decision_date ON planning_applications(decision_date);
        CREATE INDEX idx_pa_appeal_ref ON planning_applications(appeal_ref_number);
        CREATE INDEX idx_pa_one_off_house ON planning_applications(one_off_house);
        COMMIT;
        """
    )

    # council_refusal_reasons: rebuild without fetched_at / classified_at
    cols = ", ".join(KEEP_CRR_COLUMNS)
    conn.executescript(
        f"""
        BEGIN;
        CREATE TABLE council_refusal_reasons_new (
            id INTEGER PRIMARY KEY,
            object_id INTEGER,
            reason_number INTEGER,
            short_prescription TEXT,
            raw_text TEXT,
            summary TEXT,
            dev_plan TEXT,
            policy_codes TEXT,
            quantitative_violation TEXT,
            statutory_test TEXT
        );
        INSERT INTO council_refusal_reasons_new ({cols})
            SELECT {cols} FROM council_refusal_reasons;
        DROP TABLE council_refusal_reasons;
        ALTER TABLE council_refusal_reasons_new RENAME TO council_refusal_reasons;
        CREATE INDEX idx_crr_object_id ON council_refusal_reasons(object_id);
        COMMIT;
        """
    )

    # Drop operational tables
    for t in OPERATIONAL_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")

    # documents: drop fetched_at (kept the table itself, it's referenced)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()]
    if "fetched_at" in cols:
        conn.execute("ALTER TABLE documents DROP COLUMN fetched_at")

    # Null out unused payload on non-refused rows (kept only for the appeal count)
    conn.execute(
        "UPDATE planning_applications "
        "SET development_description = NULL, "
        "    development_address = NULL, "
        "    link_app_details = NULL "
        "WHERE decision NOT LIKE '%Refuse%'"
    )

    conn.commit()
    conn.close()

    # VACUUM (must run on a fresh connection with no open transaction)
    conn = sqlite3.connect(dst)
    conn.execute("VACUUM")
    conn.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--src", required=True, type=Path)
    p.add_argument("--dst", required=True, type=Path)
    args = p.parse_args()

    if not args.src.exists():
        print(f"source not found: {args.src}", file=sys.stderr)
        return 1

    args.dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.src, args.dst)

    before = args.dst.stat().st_size
    slim(args.dst)
    after = args.dst.stat().st_size

    print(f"slimmed {args.src.name} -> {args.dst}")
    print(f"  {before / 1024 / 1024:.1f} MB -> {after / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
