"""Load Donegal decision PDFs (scraped via the MCP browser into data/donegal-pdfs/)
into the DB: parse reasons, write council_refusal_reasons + council_reasons_fetch.

Donegal is behind Cloudflare, so its PDFs are fetched interactively through the
MCP Playwright browser (see docs/council-scrapers.md -> Donegal) rather than by a
standalone HTTP scraper. This script is the ingest half: it takes whatever clean,
verified PDFs landed on disk and loads them, reusing parse_reasons from the iDocs
scraper. Re-runnable; only touches Donegal rows for refs present on disk.

Integrity gate: each PDF MUST contain its own ref (slash form "26/60542" or the
bare ref) — guards against the ViewFile.aspx stale-cache race that can serve one
ref's PDF for another. Files failing the gate are skipped (not written).
"""
from __future__ import annotations

import importlib.util
import re
from datetime import datetime, timezone
from pathlib import Path

from acp_decisions.db import open_db
from acp_decisions.pdf_parser import _extract_text

_spec = importlib.util.spec_from_file_location(
    "fetch_idocs_reasons", str(Path(__file__).with_name("fetch-idocs-reasons.py"))
)
_idocs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_idocs)
parse_reasons = _idocs.parse_reasons

COUNCIL = "Donegal County Council"
PDF_DIR = Path(__file__).resolve().parent.parent / "data" / "donegal-pdfs"


def main() -> int:
    conn = open_db("acp.db")
    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    print(f"loading {len(pdfs)} Donegal PDFs", flush=True)

    n_ok = n_empty = n_noref = n_badref = 0
    for f in pdfs:
        ref = f.stem
        row = conn.execute(
            "SELECT object_id FROM planning_applications "
            "WHERE planning_authority = ? AND application_number = ?",
            (COUNCIL, ref),
        ).fetchone()
        if not row:
            n_noref += 1
            print(f"  {ref}: no matching application_number in DB", flush=True)
            continue
        object_id = row["object_id"]

        data = f.read_bytes()
        txt = _extract_text(data)
        slash = ref[:2] + "/" + ref[2:]
        if slash not in txt and not re.search(r"\b" + re.escape(ref) + r"\b", txt):
            n_badref += 1
            print(f"  {ref}: ref not found in PDF text — skipping (possible contamination)", flush=True)
            continue

        reasons = parse_reasons(txt)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("DELETE FROM council_refusal_reasons WHERE object_id = ?", (object_id,))
        for n, rtxt in enumerate(reasons, 1):
            conn.execute(
                "INSERT INTO council_refusal_reasons "
                "(object_id, reason_number, short_prescription, raw_text, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (object_id, n, None, rtxt, now),
            )
        conn.execute(
            "INSERT OR REPLACE INTO council_reasons_fetch "
            "(object_id, fetched_at, reasons_count, error_message) VALUES (?, ?, ?, ?)",
            (object_id, now, len(reasons), None if reasons else "no reasons section parsed"),
        )
        if reasons:
            n_ok += 1
        else:
            n_empty += 1
    conn.commit()
    print(f"done. with_reasons={n_ok} empty={n_empty} no_db_match={n_noref} bad_ref={n_badref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
