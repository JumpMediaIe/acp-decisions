"""Fetch refusal reasons from Galway County Council's document portal.

Galway is not on the agileapplications.ie portal. Their planning documents are
served via apps.galwaycoco.ie/ViewExternalDocuments. Each application has a
"Notification of Decision" PDF whose schedule contains numbered refusal reasons.

Flow per application:
    1. GET /ViewExternalDocuments/?RefNo={ref}
    2. Parse HTML table; pick the "Notification of Decision" row (skip the
       "to third parties" variants).
    3. GET the PDF, extract text with pypdf.
    4. Scan the text for the schedule of reasons (after "SCHEDULE REFERRED TO",
       before "Footnote:"); split into numbered items.
    5. Insert each into council_refusal_reasons; mark in council_reasons_fetch
       for idempotent resume.

After completion, runs the standard classify + categorise stages unless
--no-followup is passed.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from selectolax.parser import HTMLParser

from acp_decisions.db import open_db
# _extract_text auto-falls back to Tesseract OCR for scanned PDFs.
from acp_decisions.pdf_parser import _extract_text


BASE = "https://apps.galwaycoco.ie/ViewExternalDocuments"
USER_AGENT = (
    "planningcheck.ie scraper - public records reproduction "
    "(contact: contact@planningcheck.ie)"
)

# Document-label patterns we try, in priority order. "Notification of Decision"
# (the primary applicant copy) is the canonical refusal doc; the "to third
# parties" variant contains the same reasons but is duplicate.
NOD_PRIMARY = re.compile(r"notification of decision\b(?!\s*letter to)", re.I)
NOD_FALLBACK = re.compile(r"notification of decision", re.I)

# In older Galway docs the decision doc is labelled "R2" instead.
R2_LABEL = re.compile(r"\bR2\b", re.I)

# Where the reasons schedule starts in the PDF text.
SCHEDULE_RE = re.compile(r"schedule\s+referred\s+to", re.I)
# Footer/appeal-info text that marks the end of reasons.
END_MARKERS_RE = re.compile(
    r"footnote|an appeal against|appeal must be|coimisi[oó]n\s+plean[aá]la",
    re.I,
)
# Each reason starts with a number followed by either "." or ")",
# e.g. "1.", "1)", "2.", "2)". Galway templates have used both over the years.
REASON_NUM_RE = re.compile(r"^\s*(\d{1,2})[\.\)]\s+", re.M)


def fetch_doc_list(client: httpx.Client, ref: str) -> str | None:
    """Return HTML of the document index page, or None on failure."""
    resp = client.get(f"{BASE}/?RefNo={ref}")
    if resp.status_code != 200:
        return None
    return resp.text


def pick_decision_doc_href(html: str) -> str | None:
    """Find the document row most likely to contain refusal reasons."""
    tree = HTMLParser(html)
    primary = None
    fallback = None
    r2 = None
    for row in tree.css("tr"):
        cells_text = " | ".join((td.text() or "").strip() for td in row.css("td"))
        link = row.css_first("a")
        if not link or not link.attributes.get("href"):
            continue
        href = link.attributes["href"]
        if NOD_PRIMARY.search(cells_text) and primary is None:
            primary = href
        elif NOD_FALLBACK.search(cells_text) and fallback is None:
            fallback = href
        elif R2_LABEL.search(cells_text) and r2 is None:
            r2 = href
    return primary or fallback or r2


def extract_pdf_text(client: httpx.Client, href: str) -> str:
    pdf_resp = client.get(f"{BASE}/{href}")
    if pdf_resp.status_code != 200 or not pdf_resp.content:
        return ""
    try:
        # Uses pypdf first; falls back to Tesseract OCR when pypdf
        # yields <500 chars (i.e. the PDF is a scanned image).
        return _extract_text(pdf_resp.content)
    except Exception:
        return ""


def _normalise(body: str) -> str:
    body = body.replace("\r", "").strip()
    body = re.sub(r"[“”]", '"', body)
    body = re.sub(r"[‘’]", "'", body)
    body = re.sub(r"\s+\n\s+", "\n", body)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    # PDF mojibake: 'â' often replaces a dash; '�' is a generic replacement.
    body = body.replace("�", "-")
    return body


def parse_reasons(text: str) -> list[str]:
    """Split the schedule into a list of reason texts, in order.

    Handles three Galway templates:
        1. Modern multi-reason: "SCHEDULE REFERRED TO ..." then "1.", "2." ...
        2. Modern single-reason: "SCHEDULE REFERRED TO ..." then unnumbered prose
        3. Older inline: "For the reason(s) set out hereunder;" then prose

    Returns an empty list if no recognisable schedule is found.
    """
    schedule = None

    m = SCHEDULE_RE.search(text)
    if m:
        # The "SCHEDULE REFERRED TO" header is usually followed by "PLANNING
        # REFERENCE NO. ..."; skip past that line if present.
        after = text[m.end():]
        # Strip leading "- planning reference no. NN/NN" line if present.
        after = re.sub(
            r"^\s*[-:]?\s*planning\s+reference\s+no\.?[^\n]*\n",
            "",
            after,
            count=1,
            flags=re.I,
        )
        schedule = after
    else:
        # Older Extension-of-Duration template uses inline reasons.
        m2 = re.search(
            r"for\s+the\s+reason\(?s\)?\s+set\s+out\s+hereunder[\s:;,]*",
            text,
            re.I,
        )
        if m2:
            schedule = text[m2.end():]

    if schedule is None:
        return []

    end = END_MARKERS_RE.search(schedule)
    if end:
        schedule = schedule[:end.start()]

    splits = list(REASON_NUM_RE.finditer(schedule))
    if splits:
        out: list[str] = []
        for i, mm in enumerate(splits):
            start = mm.end()
            next_start = splits[i + 1].start() if i + 1 < len(splits) else len(schedule)
            body = _normalise(schedule[start:next_start])
            if len(body) >= 30:
                out.append(body)
        if out:
            return out

    # No numbered markers — single reason. Return the whole schedule as one.
    single = _normalise(schedule)
    if len(single) >= 50:
        return [single]
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="acp.db")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds between portal hits (default 1.0).")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--retry", action="store_true",
                        help="Retry rows previously recorded with an error.")
    parser.add_argument("--from-date", type=str, default="2011-01-01")
    parser.add_argument("--no-followup", action="store_true",
                        help="Skip classify + categorise.")
    args = parser.parse_args()

    conn = open_db(args.db)

    where_extra = ""
    if not args.retry:
        where_extra = (
            " AND NOT EXISTS ("
            "  SELECT 1 FROM council_reasons_fetch f "
            "    WHERE f.object_id = pa.object_id"
            " )"
        )
    else:
        where_extra = (
            " AND NOT EXISTS ("
            "  SELECT 1 FROM council_reasons_fetch f "
            "    WHERE f.object_id = pa.object_id "
            "      AND (f.error_message IS NULL OR f.error_message = '')"
            " )"
        )

    sql = (
        "SELECT pa.object_id, pa.application_number "
        "  FROM planning_applications pa "
        " WHERE pa.planning_authority = 'Galway County Council' "
        "   AND pa.decision LIKE '%Refuse%' "
        "   AND pa.application_number IS NOT NULL "
        "   AND pa.application_number != '' "
        "   AND pa.decision_date >= ? "
        f"  {where_extra} "
        " ORDER BY pa.decision_date DESC"
    )
    if args.limit > 0:
        sql += f" LIMIT {args.limit}"

    rows = conn.execute(sql, (args.from_date,)).fetchall()
    print(f"to fetch: {len(rows):,} Galway refusals", flush=True)
    if not rows:
        return 0

    n_with_reasons = 0
    n_empty = 0
    n_error = 0
    t0 = time.monotonic()

    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as client:
        try:
            for i, r in enumerate(rows, 1):
                object_id = r["object_id"]
                ref = r["application_number"]
                error_msg: str | None = None
                reasons: list[str] = []

                try:
                    html = fetch_doc_list(client, ref)
                    if html is None:
                        error_msg = "doc list HTTP non-200"
                    else:
                        href = pick_decision_doc_href(html)
                        if href is None:
                            error_msg = "no decision document on portal"
                        else:
                            time.sleep(args.delay)
                            text = extract_pdf_text(client, href)
                            if not text:
                                error_msg = "pdf download or text extraction failed"
                            else:
                                reasons = parse_reasons(text)
                                if not reasons:
                                    error_msg = "no schedule of reasons parsed"
                except httpx.HTTPError as e:
                    error_msg = str(e)[:300]
                except Exception as e:
                    error_msg = f"{type(e).__name__}: {e}"[:300]

                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "DELETE FROM council_refusal_reasons WHERE object_id = ?",
                    (object_id,),
                )
                for n, reason_text in enumerate(reasons, 1):
                    conn.execute(
                        """
                        INSERT INTO council_refusal_reasons
                            (object_id, reason_number, short_prescription, raw_text, fetched_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (object_id, n, None, reason_text, now),
                    )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO council_reasons_fetch
                        (object_id, fetched_at, reasons_count, error_message)
                    VALUES (?, ?, ?, ?)
                    """,
                    (object_id, now, len(reasons), error_msg),
                )

                if reasons:
                    n_with_reasons += 1
                elif error_msg is None or error_msg == "no schedule of reasons parsed":
                    n_empty += 1
                else:
                    n_error += 1

                time.sleep(args.delay)

                if i % 25 == 0:
                    conn.commit()
                    rate = i / (time.monotonic() - t0)
                    eta = (len(rows) - i) / rate / 60
                    print(
                        f"  {i:,}/{len(rows):,}  with_reasons={n_with_reasons:,} "
                        f"empty={n_empty:,} errors={n_error:,}  "
                        f"~{rate:.2f}/s ETA {eta:.0f} min",
                        flush=True,
                    )
        finally:
            conn.commit()

    print(
        f"done. with_reasons={n_with_reasons:,} empty={n_empty:,} errors={n_error:,}"
    )

    if not args.no_followup and n_with_reasons > 0:
        _run_followup(args.db)
    return 0


def _run_followup(db_path: str) -> None:
    scripts_dir = Path(__file__).parent
    for stage in ("classify-council-reasons.py", "categorize-council-reasons.py"):
        path = scripts_dir / stage
        print(f"\n=== auto: {stage} ===", flush=True)
        try:
            subprocess.run([sys.executable, str(path), "--db", db_path], check=True)
        except subprocess.CalledProcessError as e:
            print(f"  {stage} failed (exit {e.returncode}); continuing")


if __name__ == "__main__":
    sys.exit(main())
