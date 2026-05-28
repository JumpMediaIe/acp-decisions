"""Fetch refusal reasons from Louth County Council's iDocs document portal.

Louth hosts decisions at apps.louthcoco.ie/idocswebDPSS — the same
iDocsWebDPSS platform Kildare, Meath and Wicklow use, just on a different
subdomain. The path to a single PDF is a 4-hop chain because the legacy
ASP.NET WebForms app wraps the file in two layers of iframes and requires
a session cookie set by listFiles.

Flow per application:
    1. GET listFiles.aspx?catalog=planning&id={ref}         (sets session cookie)
       Parse the table for the "Notification of Decision" row, extract docid.
    2. GET ViewFiles.aspx?docid={docid}&format=djvu
       The body contains an iframe whose src points at ViewPdf.aspx?...&file=...pdf.
    3. GET that ViewPdf.aspx URL
       Its body contains an inner iframe whose src is the real PDF path
       (./files/<uuid>.pdf).
    4. GET the real PDF
       Run pdf_parser._extract_text (pypdf with Tesseract OCR fallback).

Reasons section markers seen in Meath PDFs (same as Kildare):
    - "Permission is REFUSED for the following reasons:"  (modern template)
    - "for the reason(s) set out hereunder"               (rare, older template)

Reasons are numbered "1.", "2." etc. Single-reason docs may omit the number.
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
from acp_decisions.pdf_parser import _extract_text


BASE = "https://apps.louthcoco.ie/idocswebDPSS"
USER_AGENT = (
    "planningcheck.ie scraper - public records reproduction "
    "(contact: contact@planningcheck.ie)"
)

# Document-row labels to prefer.
NOD_PRIMARY = re.compile(r"^\s*notification of decision\s*$", re.I)
SCHEDULE_DOC = re.compile(r"schedule of conditions", re.I)
NOD_FALLBACK = re.compile(r"notification of decision", re.I)
_FILE_REF_RE = re.compile(r"files[\\/]+([0-9a-fA-F][0-9a-fA-F-]+\.(?:pdf|djvu))", re.I)
CEO_ORDER = re.compile(r"chief executive[''s]*\s+order", re.I)

# Where the reasons section starts. Louth uses "SCHEDULE" on its own line
# after a "REFERENCE NO. X/Y" header. parse_reasons uses the LAST occurrence
# so body mentions of "schedule" don't trip us up.
REASONS_START_RE = re.compile(
    r"(reasons?\s+for\s+refusal\b|"
    r"permission\s+is\s+refused\s+for\s+the\s+following\s+reason[s]?\b|"
    r"for\s+the\s+reason\(?s\)?\s+set\s+out\s+hereunder|"
    r"refused\s+for\s+the\s+following\s+reason[s]?\b|"
    r"reference\s+no\.?\s*[\d/]+|"
    r"\bschedule\b)",
    re.I,
)
END_MARKERS_RE = re.compile(
    r"footnote|an appeal against|signed\s+(this|on\s+behalf)|coimisi[oó]n\s+plean[aá]la|"
    r"date:\s*\d{1,2}/\d{1,2}/\d{2,4}",
    re.I,
)
REASON_NUM_RE = re.compile(r"^\s*(\d{1,2})[\.\)]\s+", re.M)


def _normalise(body: str) -> str:
    body = body.replace("\r", "").strip()
    body = re.sub(r"[“”]", '"', body)
    body = re.sub(r"[‘’]", "'", body)
    body = re.sub(r"\s+\n\s+", "\n", body)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = body.replace("�", "-")
    return body


def parse_reasons(text: str) -> list[str]:
    # Use LAST occurrence: body text may mention "Schedule hereto" earlier,
    # but the real section header (or REFERENCE NO. line) always comes later.
    last_match = None
    for mm in REASONS_START_RE.finditer(text):
        last_match = mm
    if last_match is None:
        return []
    schedule = text[last_match.end():]
    end = END_MARKERS_RE.search(schedule)
    if end:
        schedule = schedule[:end.start()]
    splits = list(REASON_NUM_RE.finditer(schedule))
    if splits:
        out: list[str] = []
        for i, mm in enumerate(splits):
            start = mm.end()
            nxt = splits[i + 1].start() if i + 1 < len(splits) else len(schedule)
            body = _normalise(schedule[start:nxt])
            if len(body) >= 30:
                out.append(body)
        if out:
            return out
    single = _normalise(schedule)
    if len(single) >= 50:
        return [single]
    return []


def fetch_nod_docid(client: httpx.Client, ref: str) -> str | None:
    """Hit listFiles, return the docid of the primary Notification of Decision."""
    resp = client.get(f"{BASE}/listFiles.aspx?catalog=planning&id={ref}")
    if resp.status_code != 200:
        return None
    tree = HTMLParser(resp.text)
    primary = None
    schedule = None
    fallback = None
    for row in tree.css("tr"):
        cells = row.css("td")
        # Louth doc rows have 5 cells; skip the giant wrapper <tr>.
        if not (5 <= len(cells) <= 6):
            continue
        label = (cells[0].text() or "").strip()
        if len(label) > 80:
            continue
        link = row.css_first("a")
        if not link or not link.attributes.get("href"):
            continue
        href = link.attributes["href"]
        m = re.search(r"docid=(\d+)", href)
        if not m:
            continue
        docid = m.group(1)
        if NOD_PRIMARY.match(label) and primary is None:
            primary = docid
        elif SCHEDULE_DOC.search(label) and schedule is None:
            schedule = docid
        elif NOD_FALLBACK.search(label) and fallback is None:
            fallback = docid
    return primary or schedule or fallback


def fetch_pdf_bytes(client: httpx.Client, docid: str) -> bytes:
    """Walk the viewer chain and return the underlying PDF/DjVu bytes (b'' on failure)."""
    r1 = client.get(f"{BASE}/ViewFiles.aspx?docid={docid}&format=djvu")
    if r1.status_code != 200:
        return b""
    t1 = HTMLParser(r1.text)
    iframe = t1.css_first("iframe")
    if not iframe or not iframe.attributes.get("src"):
        return b""
    r2 = client.get(f"{BASE}/{iframe.attributes['src']}")
    if r2.status_code != 200:
        return b""
    m = _FILE_REF_RE.search(r2.text)
    if not m:
        return b""
    r3 = client.get(f"{BASE}/files/{m.group(1)}")
    ct = r3.headers.get("content-type", "")
    if r3.status_code != 200:
        return b""
    if not (ct.startswith("application/pdf") or "djvu" in ct):
        return b""
    return r3.content


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="acp.db")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--from-date", type=str, default="2011-01-01")
    parser.add_argument("--no-followup", action="store_true")
    args = parser.parse_args()

    conn = open_db(args.db)

    if args.retry:
        where_extra = (
            " AND NOT EXISTS ("
            "  SELECT 1 FROM council_reasons_fetch f "
            "    WHERE f.object_id = pa.object_id "
            "      AND (f.error_message IS NULL OR f.error_message = '')"
            " )"
        )
    else:
        where_extra = (
            " AND NOT EXISTS ("
            "  SELECT 1 FROM council_reasons_fetch f "
            "    WHERE f.object_id = pa.object_id"
            " )"
        )

    sql = (
        "SELECT pa.object_id, pa.application_number "
        "  FROM planning_applications pa "
        " WHERE pa.planning_authority = 'Louth County Council' "
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
    print(f"to fetch: {len(rows):,} Louth refusals", flush=True)
    if not rows:
        return 0

    n_with_reasons = 0
    n_empty = 0
    n_error = 0
    t0 = time.monotonic()

    with httpx.Client(timeout=60, follow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as client:
        try:
            for i, r in enumerate(rows, 1):
                object_id = r["object_id"]
                ref = r["application_number"]
                error_msg: str | None = None
                reasons: list[str] = []

                try:
                    docid = fetch_nod_docid(client, ref)
                    if not docid:
                        error_msg = "no Notification of Decision on portal"
                    else:
                        time.sleep(args.delay)
                        pdf_bytes = fetch_pdf_bytes(client, docid)
                        if not pdf_bytes:
                            error_msg = "pdf fetch failed in iframe chain"
                        else:
                            txt = _extract_text(pdf_bytes)
                            if not txt:
                                error_msg = "pdf text extraction empty (incl OCR)"
                            else:
                                reasons = parse_reasons(txt)
                                if not reasons:
                                    error_msg = "no reasons section parsed"
                except httpx.HTTPError as e:
                    error_msg = str(e)[:300]
                except Exception as e:  # noqa: BLE001
                    error_msg = f"{type(e).__name__}: {e}"[:300]

                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "DELETE FROM council_refusal_reasons WHERE object_id = ?",
                    (object_id,),
                )
                for n, txt in enumerate(reasons, 1):
                    conn.execute(
                        """
                        INSERT INTO council_refusal_reasons
                            (object_id, reason_number, short_prescription, raw_text, fetched_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (object_id, n, None, txt, now),
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
                elif error_msg == "no reasons section parsed":
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
        scripts_dir = Path(__file__).parent
        for stage in ("classify-council-reasons.py", "categorize-council-reasons.py"):
            print(f"\n=== auto: {stage} ===", flush=True)
            try:
                subprocess.run(
                    [sys.executable, str(scripts_dir / stage), "--db", args.db],
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                print(f"  {stage} failed (exit {e.returncode}); continuing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
