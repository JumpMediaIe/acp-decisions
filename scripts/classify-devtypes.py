"""One-off: use Gemma 4 (or any LLM client) to classify the development_type_id
of LGMA planning_applications rows that the regex couldn't categorise.

Reads only rows where:
    decision LIKE '%Refuse%' AND development_type_id IS NULL

Idempotent — re-running picks up only what's still unmapped.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time

import httpx

from acp_decisions.classifier import OllamaClient


# Canonical dev-type IDs the LLM may return. Must match keys used by
# devtype_map.py / the UI's DEV_TYPE_LABELS. The LLM picks ONE.
ALLOWED_IDS = [
    "large_residential_development",
    "multi_unit_housing",
    "new_house_rural",
    "demolition_replacement_dwelling",
    "house_extension_rear",
    "house_extension_side",
    "attic_conversion",
    "porch",
    "garage_shed_outbuilding",
    "driveway",
    "site_access_boundary",
    "agricultural_building",
    "equestrian",
    "quarry",
    "retention",
    "change_of_use_general",
    "restaurant_pub",
    "retail",
    "office",
    "industrial_warehouse",
    "hotel",
    "caravan_camping",
    "service_station",
    "data_centre",
    "leisure_sports",
    "school_education",
    "childcare",
    "nursing_care_home",
    "student_accommodation",
    "religious",
    "cemetery",
    "solar_panels_house",
    "solar_farm",
    "wind_turbine_single",
    "wind_farm",
    "heat_pump_air",
    "battery_storage",
    "renewable_other",
    "telecoms",
    "signage",
]

PROMPT_PRELUDE = (
    "You categorise Irish planning-application descriptions. Given the "
    "description below, pick the SINGLE best development_type_id from the "
    "list of allowed IDs.\n\n"
    "Rules:\n"
    "- Use ONE id from the allowed list. No new ids.\n"
    "- If the description has multiple components (e.g. demolition + new "
    'house), pick the *primary* component.\n'
    '- If genuinely unclassifiable, return "_other".\n\n'
    f"Allowed IDs:\n{', '.join(ALLOWED_IDS)}\n\n"
)


def classify_one(client: OllamaClient, description: str) -> str | None:
    """Ask the LLM to pick a dev_type_id. Return None if ambiguous/unknown."""
    prompt = (
        PROMPT_PRELUDE
        + f'Description: """\n{description.strip()}\n"""\n\n'
        + 'Respond with a single JSON object: {"id": "..."}'
    )
    try:
        raw = client.generate(prompt)
        parsed = json.loads(raw)
    except (json.JSONDecodeError, httpx.HTTPError, KeyError):
        return None
    if not isinstance(parsed, dict):
        return None
    chosen = parsed.get("id")
    if isinstance(chosen, str) and chosen in ALLOWED_IDS:
        return chosen
    return None  # treat _other or invalid as "still unmapped"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="acp.db")
    parser.add_argument("--model", default="gemma4:e2b")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only classify N rows (0 = all unmapped). Useful for trial runs.",
    )
    parser.add_argument(
        "--authority",
        type=str,
        default=None,
        help="Restrict to a single planning_authority.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Require >= 80 chars of description; anything shorter is LGMA boilerplate
    # ("Permission for development. The development will consist of") that the
    # LLM can't meaningfully classify either.
    where = "decision LIKE '%Refuse%' AND development_type_id IS NULL AND development_description IS NOT NULL AND length(development_description) >= 80"
    params: list = []
    if args.authority:
        where += " AND planning_authority = ?"
        params.append(args.authority)
    sql = f"SELECT object_id, development_description FROM planning_applications WHERE {where}"
    if args.limit > 0:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql, params).fetchall()
    print(f"to classify: {len(rows):,} unmapped refusals", flush=True)

    client = OllamaClient(model=args.model)
    n_classified = 0
    n_skipped = 0
    t0 = time.monotonic()
    try:
        for i, r in enumerate(rows, 1):
            chosen = classify_one(client, r["development_description"])
            if chosen is not None:
                conn.execute(
                    "UPDATE planning_applications SET development_type_id = ? WHERE object_id = ?",
                    (chosen, r["object_id"]),
                )
                n_classified += 1
            else:
                n_skipped += 1
            if i % 100 == 0:
                conn.commit()
                rate = i / (time.monotonic() - t0)
                eta_min = (len(rows) - i) / rate / 60
                print(f"  {i:,}/{len(rows):,}  classified={n_classified:,} skipped={n_skipped:,}  ~{rate:.1f}/s ETA {eta_min:.0f} min", flush=True)
    finally:
        conn.commit()
        client.close()

    print(f"done. classified={n_classified:,} skipped={n_skipped:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
