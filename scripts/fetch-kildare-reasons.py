"""Fetch refusal reasons from Kildare County Council's iDocs document portal.

Kildare hosts decisions at idocsweb.kildarecoco.ie/iDocsWebDPSS. The path to a
single PDF is a 4-hop chain because the legacy ASP.NET WebForms app wraps the
file in two layers of iframes and requires a session cookie set by listFiles.

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

Reasons section markers seen in Kildare PDFs:
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


BASE = "https://idocsweb.kildarecoco.ie/iDocsWebDPSS"
USER_AGENT = (
    "planningcheck.ie scraper - public records reproduction "
    "(contact: contact@planningcheck.ie)"
)

# Document-row labels to prefer.
NOD_PRIMARY = re.compile(r"^\s*notification of decision\s*$", re.I)
NOD_FALLBACK = re.compile(r"notification of decision", re.I)
CEO_ORDER = re.compile(r"chief executive[''s]*\s+order", re.I)

# Where the reasons section starts.
REASONS_START_RE = re.compile(
    r"(permission\s+is\s+refused\s+for\s+the\s+following\s+reasons|"
    r"for\s+the\s+reason\(?s\)?\s+set\s+out\s+hereunder|"
    r"refused\s+for\s+the\s+following\s+reasons)",
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
    m = REASONS_START_RE.search(text)
    if not m:
        return []
    schedule = text[m.end():]
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
    fallback = None
    for row in tree.css("tr"):
        cells = row.css("td")
        if len(cells) < 6:
            continue
        label = (cells[0].text() or "").strip()
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
        elif NOD_FALLBACK.search(label) and fallback is None:
            fallback = docid
    return primary or fallback


def fetch_pdf_bytes(client: httpx.Client, docid: str) -> bytes:
    """Walk the two-iframe chain and return the underlying PDF bytes (b'' on failure)."""
    r1 = client.get(f"{BASE}/ViewFiles.aspx?docid={docid}&format=djvu")
    if r1.status_code != 200:
        return b""
    t1 = HTMLParser(r1.text)
    iframe = t1.css_first("iframe")
    if not iframe or not iframe.attributes.get("src"):
        return b""
    view_pdf_url = f"{BASE}/{iframe.attributes['src']}"
    r2 = client.get(view_pdf_url)
    if r2.status_code != 200:
        return b""
    t2 = HTMLParser(r2.text)
    inner = t2.css_first("iframe")
    if not inner or not inner.attributes.get("src"):
        return b""
    src = inner.attributes["src"].split("#")[0]
    # The inner iframe src is like ".\\files\\<uuid>.pdf"; resolve relative to BASE.
    rel = src.lstrip(".").lstrip("/").lstrip("\\").replace("\\", "/")
    r3 = client.get(f"{BASE}/{rel}")
    ct = r3.headers.get("content-type", "")
    if r3.status_code != 200 or not ct.startswith("application/pdf"):
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
        " WHERE pa.planning_authority = 'Kildare County Council' "
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
    print(f"to fetch: {len(rows):,} Kildare refusals", flush=True)
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
