#!/usr/bin/env python
"""Backfill refusal reasons from scanned 'Notification of Decision' letters (OCR).

Many older agile-portal refusals (Cork County, Cork City, South Dublin, Fingal,
Wexford …) have NO structured reasons in the conditions API, but DO have a
scanned 'Notification of Decision' PDF whose Schedule lists the refusal reasons.
This script recovers those:

  1. work list = refused agile applications with NO reasons yet, not already
     OCR-attempted (tracked in ocr_reasons_fetch);
  2. resolve the agile application id, list its documents, pick the
     'Notification of Decision' (fallback: Manager's Order / Schedule of
     Conditions);
  3. download + OCR it (pages rasterised by PyMuPDF, read by Tesseract);
  4. split the schedule into individual reasons with the local LLM (verbatim —
     it only de-noises and separates, never paraphrases);
  5. store them in council_refusal_reasons and record the attempt.

Classify + categorise run automatically afterwards (same as the API fetch),
unless --no-followup.

Polite delay between portal calls; safe to ctrl-C and resume — ocr_reasons_fetch
tracks every attempt.

Requires: Tesseract (binary), pytesseract, PyMuPDF (fitz), Pillow, and a running
Ollama with the split model (default gemma4:e2b).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from acp_decisions.agile_api import AgileApiClient, AgileApiError
from acp_decisions.classifier import OllamaClient
from acp_decisions.db import open_db


def _search_ref(slug: str | None, application_number: str) -> str:
    """Map a stored application_number to the form the Agile search expects.

    Mirrors fetch-council-reasons.py: only Cork City needs the slash inserted
    ('2543892' -> '25/43892'); every other agile council stores it searchable.
    """
    ref = (application_number or "").strip()
    if slug == "corkcity" and "/" not in ref and ref.isdigit() and len(ref) > 2:
        return ref[:2] + "/" + ref[2:]
    return ref


# --- OCR setup --------------------------------------------------------------

def _resolve_tesseract() -> None:
    """Point pytesseract at the Tesseract binary (PATH or the Windows default)."""
    import pytesseract

    if shutil.which("tesseract"):
        return  # on PATH already
    env = os.environ.get("TESSERACT_CMD")
    candidates = [
        env,
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for c in candidates:
        if c and Path(c).exists():
            pytesseract.pytesseract.tesseract_cmd = c
            return
    raise RuntimeError(
        "Tesseract binary not found. Install it or set TESSERACT_CMD to its path."
    )


def ocr_pdf(pdf_bytes: bytes, dpi: int = 300) -> str:
    """OCR every page of a PDF and return the concatenated text."""
    import fitz  # PyMuPDF
    import pytesseract
    from PIL import Image

    out: list[str] = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            # psm 6 (assume a uniform block of text) reads the Schedule's
            # two-column reason table far better than the default psm 3 —
            # it preserves the shaded header and the leading row numbers
            # (1. 2. 3.), which are what we split reasons on.
            out.append(pytesseract.image_to_string(img, config="--psm 6"))
    finally:
        doc.close()
    return "\n\n".join(out)


# --- document selection -----------------------------------------------------

_PRIMARY = "notification of decision"
_FALLBACK_HINTS = ("manager's order", "chief executive", "schedule of conditions")


def pick_decision_doc(docs: list[dict]) -> dict | None:
    """Pick the document most likely to contain the refusal reasons.

    Prefer the 'Notification of Decision'; fall back to the Manager's /
    Chief Executive's Order (which carries the same Schedule of reasons).
    """
    def med(d: dict) -> str:
        return (d.get("mediaDescription") or d.get("description") or "").lower()

    for d in docs:
        if _PRIMARY in med(d):
            return d
    for d in docs:
        if any(h in med(d) for h in _FALLBACK_HINTS):
            return d
    return None


# --- reason extraction (local LLM, verbatim) --------------------------------

_SPLIT_PROMPT = """You are extracting the refusal reasons from OCR text of an Irish planning authority's "Notification of Decision to Refuse" letter and its attached Schedule of reasons.

How the Schedule is laid out: it is a table titled "Refusal Reason(s)". Each row is ONE numbered reason ("1.", "2.", "3." …). A single reason often contains bullet points (rendered by OCR as "e" or "•") and several sentences/paragraphs — ALL of that belongs to the SAME reason.

Rules:
- Return ONE item per NUMBERED reason. Do NOT split a reason on its bullets, sentences or paragraphs. Do NOT merge two different numbered reasons.
- Reproduce each reason as written by the planning authority. Do NOT summarise, rephrase or shorten. You MAY lightly correct obvious OCR mis-reads (e.g. "cffect"->"effect", "Harner"->"Harrier", "satisfi"->"satisfied") and drop stray single garbage characters, but never change the wording or meaning.
- Drop the leading row number ("1.") from each reason's text.
- IGNORE entirely: the cover letter (applicant name/address, "decided to REFUSE", "for the reasons set out in the Schedule"), appeal-notice boilerplate, page headers/footers, document filenames like "DD1.R.084287.doc", and the shaded "Refusal Reason(s)" header itself.
- If the text contains no actual refusal reasons, return an empty list.

Return ONLY JSON in this exact shape: {"reasons": ["<reason 1 text>", "<reason 2 text>"]}

OCR TEXT:
---
%s
---"""

_MIN_REASON_CHARS = 30
_MAX_REASONS = 15


def extract_reasons(llm: OllamaClient, ocr_text: str) -> list[str]:
    """Split OCR text into clean, verbatim refusal reasons via the local LLM."""
    raw = llm.generate(_SPLIT_PROMPT % ocr_text[:12000])
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    reasons = data.get("reasons") if isinstance(data, dict) else None
    if not isinstance(reasons, list):
        return []
    cleaned: list[str] = []
    for r in reasons:
        if not isinstance(r, str):
            continue
        t = " ".join(r.split()).strip()
        if len(t) >= _MIN_REASON_CHARS:
            cleaned.append(t)
        if len(cleaned) >= _MAX_REASONS:
            break
    return cleaned


def _resolve_with_retry(api: AgileApiClient, slug: str, ref: str, tries: int = 3) -> int | None:
    """search_application_id with backoff — the search endpoint throttles."""
    for attempt in range(tries):
        try:
            app_id = api.search_application_id(slug, ref)
            if app_id is not None:
                return app_id
        except (AgileApiError, httpx.HTTPError):
            pass
        time.sleep(1.0 * (attempt + 1))
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="acp.db")
    parser.add_argument("--council", default=None,
                        help="Limit to one planning_authority (substring match), "
                             "e.g. 'Cork County'. Default: all agile councils.")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds between applications (default 1s).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after N applications (0 = all).")
    parser.add_argument("--from-date", default=None,
                        help="Only applications decided on/after YYYY-MM-DD.")
    parser.add_argument("--to-date", default=None,
                        help="Only applications decided on/before YYYY-MM-DD.")
    parser.add_argument("--dpi", type=int, default=300, help="OCR rasterisation DPI.")
    parser.add_argument("--model", default="gemma4:e2b",
                        help="Ollama model for the verbatim reason split.")
    parser.add_argument("--retry", action="store_true",
                        help="Also retry rows previously attempted with an error.")
    parser.add_argument("--no-followup", action="store_true",
                        help="Skip the classify + categorise stages.")
    args = parser.parse_args()

    _resolve_tesseract()

    conn = open_db(args.db)

    if args.retry:
        skip = (" AND NOT EXISTS (SELECT 1 FROM ocr_reasons_fetch f "
                " WHERE f.object_id = pa.object_id "
                " AND (f.error_message IS NULL OR f.error_message = '')) ")
    else:
        skip = (" AND NOT EXISTS (SELECT 1 FROM ocr_reasons_fetch f "
                " WHERE f.object_id = pa.object_id) ")

    council_clause = ""
    params: list = []
    if args.council:
        council_clause = " AND pa.planning_authority LIKE ? "
        params.append(f"%{args.council}%")
    if args.from_date:
        council_clause += " AND pa.decision_date >= ? "
        params.append(args.from_date)
    if args.to_date:
        council_clause += " AND pa.decision_date <= ? "
        params.append(args.to_date)

    sql = (
        "SELECT pa.object_id, pa.application_number, pa.link_app_details "
        "  FROM planning_applications pa "
        " WHERE pa.decision LIKE '%Refuse%' "
        "   AND pa.link_app_details LIKE '%agileapplications%' "
        "   AND pa.application_number IS NOT NULL AND pa.application_number != '' "
        # only applications that have no structured reasons yet
        "   AND NOT EXISTS (SELECT 1 FROM council_refusal_reasons r "
        "                   WHERE r.object_id = pa.object_id) "
        f"  {council_clause} {skip} "
        " ORDER BY pa.decision_date DESC "
    )
    if args.limit > 0:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql, params).fetchall()
    print(f"to OCR: {len(rows):,} candidate applications", flush=True)

    api = AgileApiClient()
    llm = OllamaClient(model=args.model)
    n_reasons = n_no_doc = n_no_id = n_empty = n_error = 0
    t0 = time.monotonic()
    try:
        for i, r in enumerate(rows, 1):
            object_id = r["object_id"]
            slug = api.slug_from_link(r["link_app_details"])
            ref = _search_ref(slug, r["application_number"])
            error_msg: str | None = None
            doc_title: str | None = None
            reasons: list[str] = []
            try:
                if not slug:
                    raise AgileApiError("could not extract slug from link")
                app_id = _resolve_with_retry(api, slug, ref)
                if app_id is None:
                    raise AgileApiError("not found via search")
                docs = api.list_documents(slug, app_id)
                doc = pick_decision_doc(docs)
                if not doc:
                    n_no_doc += 1
                else:
                    doc_title = (doc.get("mediaDescription") or "").strip() or None
                    pdf = api.download_document(slug, doc["documentHash"])
                    if pdf[:4] != b"%PDF":
                        raise AgileApiError("download was not a PDF")
                    text = ocr_pdf(pdf, dpi=args.dpi)
                    reasons = extract_reasons(llm, text)
            except (AgileApiError, httpx.HTTPError) as e:
                error_msg = str(e)[:300]
            except Exception as e:  # OCR / decode / LLM errors — log, keep going
                error_msg = f"{type(e).__name__}: {e}"[:300]

            now = datetime.now(timezone.utc).isoformat()
            if reasons:
                conn.execute("DELETE FROM council_refusal_reasons WHERE object_id = ?", (object_id,))
                for n, text in enumerate(reasons, 1):
                    conn.execute(
                        "INSERT INTO council_refusal_reasons "
                        "(object_id, reason_number, short_prescription, raw_text, fetched_at) "
                        "VALUES (?, ?, NULL, ?, ?)",
                        (object_id, n, text, now),
                    )
                n_reasons += 1
            elif error_msg is None and doc_title is not None:
                n_empty += 1  # had a doc but OCR/LLM found no reasons
            elif error_msg and "not found via search" in error_msg:
                n_no_id += 1
            elif error_msg:
                n_error += 1

            conn.execute(
                "INSERT INTO ocr_reasons_fetch "
                "(object_id, fetched_at, reasons_count, doc_title, error_message) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(object_id) DO UPDATE SET "
                "fetched_at=excluded.fetched_at, reasons_count=excluded.reasons_count, "
                "doc_title=excluded.doc_title, error_message=excluded.error_message",
                (object_id, now, len(reasons), doc_title, error_msg),
            )
            conn.commit()

            if i % 25 == 0 or i == len(rows):
                rate = i / max(1e-9, time.monotonic() - t0)
                print(
                    f"  {i}/{len(rows)}  +reasons={n_reasons} no_doc={n_no_doc} "
                    f"empty={n_empty} no_id={n_no_id} err={n_error}  ({rate:.2f}/s)",
                    flush=True,
                )
            time.sleep(args.delay)
    finally:
        api.close()
        llm.close()

    print(
        f"\nDONE: {n_reasons} apps got reasons, {n_no_doc} had no decision doc, "
        f"{n_empty} doc-but-empty, {n_no_id} unresolved, {n_error} errors.",
        flush=True,
    )

    if not args.no_followup and n_reasons > 0:
        _run_followup_stages(args.db)
    return 0


def _run_followup_stages(db_path: str) -> None:
    """Run classify + categorise on the newly-stored reasons (best-effort)."""
    scripts_dir = Path(__file__).parent
    for stage in ("classify-council-reasons.py", "categorize-council-reasons.py"):
        path = scripts_dir / stage
        print(f"\n=== auto: {stage} ===", flush=True)
        try:
            subprocess.run([sys.executable, str(path), "--db", db_path], check=False)
        except Exception as e:  # noqa: BLE001
            print(f"  {stage} failed: {e}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
