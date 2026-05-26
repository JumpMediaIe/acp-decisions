"""Fetch refusal reasons from Kildare's older 'Schedule of Conditions' docs.

For pre-2020 Kildare applications the doc labelled 'Notification of Decision
Letters' is just the brief notice letter — the actual refusal reasons live in
a separate 'Schedule of Conditions' doc on the same page.

This script targets that doc specifically. It only runs on rows that
previously came back empty or with an error from fetch-kildare-reasons.py,
so it complements rather than re-doing the modern-doc pass.

Doc structure:
    <Project description ending with — applicant — ref>
    1. <reason text>
    2. <reason text>
    ...

(No "REFUSED for the following reasons" preamble; numbered items start
straight after the project header.)
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

# Older docs use varied labels; we try them in priority order.
SCHEDULE_LABEL = re.compile(r"^\s*schedule of conditions\s*$", re.I)
DECISION_LABEL = re.compile(r"^\s*decision\s*$", re.I)

# Numbered items at line start; allow "." or ")" after the digit.
REASON_NUM_RE = re.compile(r"^\s*(\d{1,2})[\.\)]\s+", re.M)

# Where the reasons end. Older docs sometimes append signatures or boilerplate.
END_MARKERS_RE = re.compile(
    r"signed\s+(this|on\s+behalf)|county\s+secretary|pp\s+county|"
    r"please\s+see\s+attached\s+sheet|"
    r"an appeal against|coimisi[oó]n\s+plean[aá]la",
    re.I,
)


def _normalise(body: str) -> str:
    body = body.replace("\r", "").strip()
    body = re.sub(r"[“”]", '"', body)
    body = re.sub(r"[‘’]", "'", body)
    body = re.sub(r"\s+\n\s+", "\n", body)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = body.replace("�", "-")
    return body


# Markers that immediately precede the real numbered refusal reasons.
# "Planning Permission is sought for ..." appears twice in a Decision doc:
# once briefly in the header and once at the start of the schedule of reasons.
# The LATER occurrence is the one we want.
_REASONS_ANCHOR_RE = re.compile(
    r"planning permission is sought for|"
    r"retention permission is sought for|"
    r"refused for the following",
    re.I,
)
# Where the reasons section ends.
_TRAILING_END_RE = re.compile(
    r"\n\s*(signed|date:|kildare\s+county\s+council)",
    re.I,
)


def parse_reasons(text: str) -> list[str]:
    """Parse numbered refusal reasons from a Decision / Schedule of Conditions doc.

    These docs typically contain TWO numbered lists: an "appeal procedure"
    preamble (1. Confirmation of submission... 2. Statutory fee...) followed
    by the actual refusal reasons after a "Planning Permission is sought for..."
    restatement of the project.

    Strategy:
      1. Skip past the appeal preamble by finding a "Planning Permission is
         sought for" anchor (or one of its variants) — the real reasons come
         after that.
      2. From there, split on line-anchored numbered markers.
    """
    # Cut off everything before the LATER project-restatement anchor — the
    # earlier mention is in the brief header, the later one introduces the
    # numbered schedule of reasons.
    anchor_end = 0
    for m in _REASONS_ANCHOR_RE.finditer(text):
        anchor_end = m.end()
    if anchor_end == 0:
        return []  # no recognisable anchor — give up
    body = text[anchor_end:]

    splits = list(REASON_NUM_RE.finditer(body))
    if not splits:
        return []
    out: list[str] = []
    for i, mm in enumerate(splits):
        start = mm.end()
        nxt = splits[i + 1].start() if i + 1 < len(splits) else len(body)
        chunk = body[start:nxt]
        end = END_MARKERS_RE.search(chunk)
        if end:
            chunk = chunk[:end.start()]
        normalised = _normalise(chunk)
        if len(normalised) >= 50:
            out.append(normalised)
    return out


def fetch_schedule_docid(client: httpx.Client, ref: str) -> str | None:
    """Pick the doc most likely to contain the refusal reasons.

    Priority: Schedule of Conditions (2017-2019), then Decision (pre-2017).
    Returns None if neither label is present.
    """
    resp = client.get(f"{BASE}/listFiles.aspx?catalog=planning&id={ref}")
    if resp.status_code != 200:
        return None
    tree = HTMLParser(resp.text)
    schedule_docid = None
    decision_docid = None
    for row in tree.css("tr"):
        cells = row.css("td")
        if len(cells) < 6:
            continue
        label = (cells[0].text() or "").strip()
        link = row.css_first("a")
        if not link or not link.attributes.get("href"):
            continue
        m = re.search(r"docid=(\d+)", link.attributes["href"])
        if not m:
            continue
        if SCHEDULE_LABEL.match(label) and schedule_docid is None:
            schedule_docid = m.group(1)
        elif DECISION_LABEL.match(label) and decision_docid is None:
            decision_docid = m.group(1)
    return schedule_docid or decision_docid


def fetch_pdf_bytes(client: httpx.Client, docid: str) -> bytes:
    """4-hop fetch chain — same as fetch-kildare-reasons.py."""
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
    t2 = HTMLParser(r2.text)
    inner = t2.css_first("iframe")
    if not inner or not inner.attributes.get("src"):
        return b""
    src = inner.attributes["src"].split("#")[0]
    rel = src.lstrip(".").lstrip("/").lstrip("\\").replace("\\", "/")
    r3 = client.get(f"{BASE}/{rel}")
    if r3.status_code != 200 or not r3.headers.get("content-type", "").startswith("application/pdf"):
        return b""
    return r3.content


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="acp.db")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-followup", action="store_true")
    args = parser.parse_args()

    conn = open_db(args.db)

    # Target ONLY Kildare refusals where fetch-kildare-reasons.py already
    # tried and came back with no reasons or an error. (Skip rows that
    # already have reasons text, since those are covered by the modern doc.)
    sql = (
        "SELECT pa.object_id, pa.application_number "
        "  FROM planning_applications pa "
        "  JOIN council_reasons_fetch f ON f.object_id = pa.object_id "
        " WHERE pa.planning_authority = 'Kildare County Council' "
        "   AND pa.decision LIKE '%Refuse%' "
        "   AND f.reasons_count = 0 "
        " ORDER BY pa.decision_date ASC"  # oldest first — "Schedule of Conditions" is the older doc label
    )
    if args.limit > 0:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql).fetchall()
    print(f"to fetch: {len(rows):,} Kildare refusals (Schedule of Conditions)", flush=True)
    if not rows:
        return 0

    n_with_reasons = 0
    n_no_schedule = 0
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
                    docid = fetch_schedule_docid(client, ref)
                    if not docid:
                        error_msg = "no Schedule of Conditions doc on portal"
                        n_no_schedule += 1
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
                                    error_msg = "no numbered reasons found"
                except httpx.HTTPError as e:
                    error_msg = str(e)[:300]
                except Exception as e:  # noqa: BLE001
                    error_msg = f"{type(e).__name__}: {e}"[:300]

                if reasons:
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
                        (object_id, now, len(reasons), None),
                    )
                    n_with_reasons += 1
                else:
                    if error_msg == "no numbered reasons found":
                        n_empty += 1
                    elif error_msg != "no Schedule of Conditions doc on portal":
                        n_error += 1

                time.sleep(args.delay)

                if i % 25 == 0:
                    conn.commit()
                    rate = i / (time.monotonic() - t0)
                    eta = (len(rows) - i) / rate / 60
                    print(
                        f"  {i:,}/{len(rows):,}  with_reasons={n_with_reasons:,} "
                        f"no_schedule={n_no_schedule:,} empty={n_empty:,} "
                        f"errors={n_error:,}  ~{rate:.2f}/s ETA {eta:.0f} min",
                        flush=True,
                    )
        finally:
            conn.commit()

    print(
        f"done. with_reasons={n_with_reasons:,} no_schedule={n_no_schedule:,} "
        f"empty={n_empty:,} errors={n_error:,}"
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
