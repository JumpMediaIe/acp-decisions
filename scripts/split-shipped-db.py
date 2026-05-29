"""Split the slimmed acp.db into a small core file plus 3 balanced shards.

Why: the single shipped acp.db has grown past 90 MB and is approaching
GitHub's 100 MB per-file hard limit. Rather than one file, ship:

    acp-core.db      - small shared tables (categories, the category-assignment
                       join, ACP appeal tables). National category rankings are
                       counted entirely from here, so they never touch a shard.
    acp-shard-1.db   - planning_applications + council_refusal_reasons for a
    acp-shard-2.db     balanced third of the councils each. These hold the bulky
    acp-shard-3.db     raw_text/OCR payload that drives file size.

The website (lib/decisions/db.ts) opens core read-only, ATTACHes the three
shards, and creates TEMP VIEWs `planning_applications` and
`council_refusal_reasons` that UNION ALL across the shards. Every existing
query then works unchanged against those view names — no query-layer rewrite.

Council -> shard assignment is balanced by refusal-reason volume (the dominant
size driver). All 31 RoI planning authorities are mapped; the mapping only
needs revisiting if a shard outgrows the others, not when new reasons arrive.

Usage:
    uv run python scripts/split-shipped-db.py --src acp.db --dst-dir ../irish-planning-tool/data
"""
from __future__ import annotations

import argparse
import subprocess
import sqlite3
import sys
import tempfile
from pathlib import Path


# Balanced by reason count (see scripts/split-shipped-db.py design notes).
# Heavy reason-bearers spread one-per-shard; zero/low-reason councils fill the
# remaining app-row weight. Reason loads come out ~13.9k / 14.4k / 14.0k.
SHARD_COUNCILS: dict[int, list[str]] = {
    1: [
        "Cork County Council",
        "Kildare County Council",
        "Dublin City Council",
        "Kerry County Council",
        "Kilkenny County Council",
        "Carlow County Council",
        "Longford County Council",
        "Leitrim County Council",
    ],
    2: [
        "Fingal County Council",
        "Galway County Council",
        "Dun Laoghaire Rathdown County Council",
        "Meath County Council",
        "Donegal County Council",
        "Limerick County Council",
        "Clare County Council",
        "Offaly County Council",
        "Sligo County Council",
    ],
    3: [
        "Wexford County Council",
        "South Dublin County Council",
        "Louth County Council",
        "Wicklow County Council",
        "Waterford City and County Council",
        "Mayo County Council",
        "Cork City Council",
        "Westmeath County Council",
        "Roscommon County Council",
        "Galway City Council",
        "Tipperary County Council",
        "Monaghan County Council",
        "Cavan County Council",
        "Laois County Council",
    ],
}

# Tables that live in the bulky shards. Everything else stays in core.
SHARD_TABLES = ("planning_applications", "council_refusal_reasons")


def _quote_list(names: list[str]) -> str:
    return ", ".join("'" + n.replace("'", "''") + "'" for n in names)


def build_core(slim_db: Path, core_db: Path) -> None:
    """Core = a copy of the slimmed DB with the two bulky tables dropped.

    Copying then dropping (rather than cherry-picking tables) preserves every
    other table's indexes automatically.
    """
    import shutil

    shutil.copyfile(slim_db, core_db)
    conn = sqlite3.connect(core_db)
    conn.execute("PRAGMA foreign_keys = OFF")
    for t in SHARD_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(core_db)
    conn.execute("VACUUM")
    conn.close()


def build_shard(slim_db: Path, shard_db: Path, councils: list[str]) -> None:
    """Shard = the two bulky tables, filtered to this shard's councils."""
    if shard_db.exists():
        shard_db.unlink()
    conn = sqlite3.connect(shard_db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(f"ATTACH '{slim_db.as_posix()}' AS src")
    council_in = _quote_list(councils)
    conn.executescript(
        f"""
        BEGIN;
        CREATE TABLE planning_applications AS
            SELECT * FROM src.planning_applications
             WHERE planning_authority IN ({council_in});
        CREATE TABLE council_refusal_reasons AS
            SELECT * FROM src.council_refusal_reasons
             WHERE object_id IN (
                 SELECT object_id FROM src.planning_applications
                  WHERE planning_authority IN ({council_in})
             );
        CREATE INDEX idx_pa_authority     ON planning_applications(planning_authority);
        CREATE INDEX idx_pa_decision_date ON planning_applications(decision_date);
        CREATE INDEX idx_pa_appeal_ref    ON planning_applications(appeal_ref_number);
        CREATE INDEX idx_pa_one_off_house ON planning_applications(one_off_house);
        CREATE INDEX idx_crr_object_id    ON council_refusal_reasons(object_id);
        COMMIT;
        """
    )
    conn.execute("DETACH src")
    conn.close()
    conn = sqlite3.connect(shard_db)
    conn.execute("VACUUM")
    conn.close()


def verify(slim_db: Path, core_db: Path, shard_dbs: list[Path]) -> None:
    """Confirm row counts add up and no council is unmapped."""
    src = sqlite3.connect(slim_db)
    src_pa = src.execute("SELECT COUNT(*) FROM planning_applications").fetchone()[0]
    src_crr = src.execute("SELECT COUNT(*) FROM council_refusal_reasons").fetchone()[0]
    all_councils = {
        r[0] for r in src.execute("SELECT DISTINCT planning_authority FROM planning_applications")
    }
    src.close()

    mapped = {c for cs in SHARD_COUNCILS.values() for c in cs}
    unmapped = all_councils - mapped
    if unmapped:
        raise SystemExit(f"ERROR: councils not assigned to any shard: {sorted(unmapped)}")

    shard_pa = shard_crr = 0
    for s in shard_dbs:
        conn = sqlite3.connect(s)
        shard_pa += conn.execute("SELECT COUNT(*) FROM planning_applications").fetchone()[0]
        shard_crr += conn.execute("SELECT COUNT(*) FROM council_refusal_reasons").fetchone()[0]
        conn.close()

    if shard_pa != src_pa:
        raise SystemExit(f"ERROR: planning_applications rows {shard_pa} != source {src_pa}")
    if shard_crr != src_crr:
        raise SystemExit(f"ERROR: council_refusal_reasons rows {shard_crr} != source {src_crr}")
    print(f"  verified: {src_pa:,} applications + {src_crr:,} reasons across shards == source")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--src", required=True, type=Path)
    p.add_argument("--dst-dir", required=True, type=Path)
    args = p.parse_args()

    if not args.src.exists():
        print(f"source not found: {args.src}", file=sys.stderr)
        return 1
    args.dst_dir.mkdir(parents=True, exist_ok=True)

    scripts_dir = Path(__file__).parent
    with tempfile.TemporaryDirectory() as td:
        slim_db = Path(td) / "slim.db"
        # Reuse the existing slimmer to strip unused columns/tables first.
        subprocess.run(
            [sys.executable, str(scripts_dir / "slim-shipped-db.py"),
             "--src", str(args.src), "--dst", str(slim_db)],
            check=True,
        )

        core_db = args.dst_dir / "acp-core.db"
        build_core(slim_db, core_db)
        print(f"  core:    {core_db.stat().st_size / 1024 / 1024:.1f} MB")

        shard_dbs = []
        for n, councils in SHARD_COUNCILS.items():
            shard_db = args.dst_dir / f"acp-shard-{n}.db"
            build_shard(slim_db, shard_db, councils)
            shard_dbs.append(shard_db)
            print(f"  shard-{n}: {shard_db.stat().st_size / 1024 / 1024:.1f} MB "
                  f"({len(councils)} councils)")

        verify(slim_db, core_db, shard_dbs)

    total = (core_db.stat().st_size + sum(s.stat().st_size for s in shard_dbs)) / 1024 / 1024
    print(f"  total shipped: {total:.1f} MB across {1 + len(shard_dbs)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
