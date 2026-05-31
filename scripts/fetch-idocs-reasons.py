"""Generic scraper for councils on the iDocsWeb / iDocsWebDPSS platform.

Several Irish councils host planning documents on the same legacy ASP.NET
iDocs product (Local Government Computer Services). They differ only by base
URL; the listFiles -> ViewFiles -> files/<uuid> chain is identical. This one
script handles all of them via --council + --base, superseding the per-council
clones (Kildare/Meath/Wicklow/Louth) for new additions.

Known bases (pass via --base):
    Kildare    https://idocsweb.kildarecoco.ie/iDocsWebDPSS
    Meath      https://idocswebdpss.meathcoco.ie/iDocsWebDPSS
    Wicklow    https://wicklowcoco.eplanning.ie/idocswebDPSS
    Louth      https://apps.louthcoco.ie/idocswebDPSS
    Kerry      https://kerrycoco.eplanning.ie/iDocsWEB
    Waterford  https://waterfordcouncil.eplanning.ie/iDocsWebDPSS
    Limerick   https://eplan.limerick.ie/iDocsWebDPSS
    Mayo       https://mayococo.eplanning.ie/iDocsWeb
    Kilkenny   https://idocsweb.kilkenny.ie

Flow per application:
    1. GET {base}/listFiles.aspx?catalog=planning&id={ref}  (sets session cookie)
       Pick the decision doc row (Notification of Decision > Schedule of
       Conditions > Decision/Letters fallback), extract its docid.
    2. GET {base}/ViewFiles.aspx?docid={docid}&format=djvu
       Regex out the files/<uuid>.(pdf|djvu) reference (covers the nested
       iframe used for PDFs and the <object>/<embed> used for DjVu).
    3. GET {base}/files/<uuid>  -> the real document bytes.
    4. _extract_text: pypdf, falling back to ddjvu+Tesseract OCR for DjVu /
       scanned PDFs.

After completion the classify + categorise stages run automatically unless
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
from acp_decisions.pdf_parser import _extract_text


USER_AGENT = (
    "planningcheck.ie scraper - public records reproduction "
    "(contact: contact@planningcheck.ie)"
)

# Document-row labels to prefer, in priority order.
NOD_PRIMARY = re.compile(r"^\s*notification of decision\s*$", re.I)
SCHEDULE_DOC = re.compile(r"schedule of conditions", re.I)
NOD_FALLBACK = re.compile(r"notification of decision", re.I)
DECISION_DOC = re.compile(r"^\s*decision\s*$", re.I)
# Older Carlow refs (pre-~2014) label the decision "Managers Order" /
# "Manager's Order" (the old term for Chief Executive's Order) and the cover
# letter just "Notification". Match both; the Order is the reasons-bearing doc.
MANAGERS_ORDER_DOC = re.compile(r"manager'?s\s+order|chief\s+executive'?s?\s+order", re.I)
NOTIFICATION_DOC = re.compile(r"^\s*notification\s*$", re.I)
# Last-resort label. Some older councils (e.g. Galway City pre-2018) file the
# decision letter under a generic "Correspondence" category with no decision-
# labelled doc at all. Only used when none of the above match, so it never
# overrides a real decision doc elsewhere.
CORRESPONDENCE_DOC = re.compile(r"^\s*correspondence\s*$", re.I)

# Where the reasons section starts. Two tiers: STRONG markers (explicit
# phrasing) are tried first; the WEAK bare-"SCHEDULE" matcher is a last resort
# for OCR'd docs that lack any explicit phrase (Wicklow). Splitting them avoids
# a bare "SCHEDULE" inside e.g. "END OF SCHEDULE" / "FIRST SCHEDULE" winning the
# last-occurrence anchor over the real reasons header (Mayo).
#   STRONG:
#     - "Schedule of Reasons for Refusal"                    (Mayo 2nd schedule)
#     - "Reasons for Refusal"                                (Meath)
#     - "Permission is REFUSED for the following reason(s)"  (Kildare modern)
#     - "for the reason(s) set out hereunder"                (Kildare older)
#     - "Reference Number in Register: NN/NN"                (Wicklow OCR anchor)
#     - "Reference No. NN"                                   (Louth)
#     - "on the grounds stipulated/set out ... Schedule"     (Carlow)
#     - "PL Ref: NN/NN  Refusal"                             (Carlow schedule head)
#   WEAK:
#     - bare "SCHEDULE" / "S C H E D U L E" / OCR "CHEDULE"  (Wicklow)
REASONS_START_STRONG = re.compile(
    r"(schedule\s+of\s+reasons?\s+for\s+refusal|"
    r"reasons?\s+for\s+refusal\b|"
    r"refusal\s+reason[s]?\b|"  # Cavan: "Refusal Reason" header (reversed, no "for")
    r"permission\s+is\s+refused\s+for\s+the\s+following\s+reason[s]?\b|"
    r"for\s+the\s+reason\(?s\)?\s+set\s+out\s+hereunder|"
    r"refused\s+for\s+the\s+following\s+reason[s]?\b|"
    r"on\s+the\s+grounds\s+(?:stipulated|set\s+out)[^.]{0,40}schedule|"  # Carlow
    r"pl\s+ref[^a-z]{0,12}refusal|"  # Carlow schedule header "PL Ref: 20/133 Refusal"
    r"reference\s+number\s+in\s+register[^a-z\d]{0,5}\d+[/\d]*|"
    r"reference\s+no\.?:?\s*[\d/]+)",  # ":?" also catches Cavan "REFERENCE NO: NNNN"
    re.I,
)
# Bare schedule, but not "END OF SCHEDULE" (negative lookbehind on "of ").
REASONS_START_WEAK = re.compile(
    r"(?<!of )\b(?:s(?:\s+)?)?c(?:\s+)?h(?:\s+)?e(?:\s+)?d(?:\s+)?u(?:\s+)?l(?:\s+)?e\b",
    re.I,
)
END_MARKERS_RE = re.compile(
    r"footnote|an appeal against|signed\s+(this|on\s+behalf)|coimisi[oó]n\s+plean[aá]la|"
    r"date:\s*\d{1,2}/\d{1,2}/\d{2,4}",
    re.I,
)
# OCR sometimes renders "1." as "l.", "I.", or "i." in scanned docs.
REASON_NUM_RE = re.compile(r"^\s*(\d{1,2}|[lLIi])[\.\)]\s+", re.M)


def _normalise(body: str) -> str:
    body = body.replace("\r", "").strip()
    body = re.sub(r"[“”]", '"', body)
    body = re.sub(r"[‘’]", "'", body)
    body = re.sub(r"\s+\n\s+", "\n", body)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = body.replace("�", "-")
    return body


def _reasons_from_anchor(text: str, marker: re.Pattern) -> list[str]:
    # Use the LAST occurrence of the marker: the body often says "set out in
    # the Schedule hereto" earlier, but the real header comes later.
    last = None
    for mm in marker.finditer(text):
        last = mm
    if last is None:
        return []
    schedule = text[last.end():]
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
    return [single] if len(single) >= 50 else []


# Real decisions top out around two dozen reasons (max observed = 24). A parse
# that yields more than this is over-splitting on noise — typically a combined
# "Correspondence" bundle (Galway City) where numbered list items from unrelated
# documents (bylaws, conditions) get mistaken for refusal reasons. Reject the
# whole parse rather than ship contaminated reasons.
_MAX_PLAUSIBLE_REASONS = 25


def parse_reasons(text: str) -> list[str]:
    # Strong explicit markers first; bare "SCHEDULE" only as a fallback.
    reasons = (_reasons_from_anchor(text, REASONS_START_STRONG)
               or _reasons_from_anchor(text, REASONS_START_WEAK))
    if len(reasons) > _MAX_PLAUSIBLE_REASONS:
        return []
    return reasons


# The viewer iframe references the actual file in one of two ways:
#   - a direct "files/<uuid>.pdf" path (most iDocs councils), or
#   - "ViewPdf.aspx?count=1&file=<uuid>.pdf" (Galway City's viewer).
# Either way the <uuid>.pdf is fetched from "{base}/files/<uuid>.pdf".
_FILE_REF_RE = re.compile(
    r"(?:files[\\/]+|file=)([0-9a-fA-F][0-9a-fA-F-]+\.(?:pdf|djvu))", re.I
)


def fetch_nod_docid(client: httpx.Client, base: str, ref: str) -> str | None:
    resp = client.get(f"{base}/listFiles.aspx?catalog=planning&id={ref}")
    if resp.status_code != 200:
        return None
    tree = HTMLParser(resp.text)
    primary = schedule = fallback = decision = correspondence = None
    managers = notification = None
    for row in tree.css("tr"):
        cells = row.css("td")
        # Data rows have 5-7 cells (Sligo's listing uses 7 columns; most use 5-6).
        # The wrapper/parent row is excluded by the docid-link + label-length
        # checks below, not the cell count.
        if not (5 <= len(cells) <= 7):
            continue
        label = (cells[0].text() or "").strip()
        if len(label) > 80:
            continue
        link = row.css_first("a")
        if not link or not link.attributes.get("href"):
            continue
        m = re.search(r"docid=(\d+)", link.attributes["href"])
        if not m:
            continue
        docid = m.group(1)
        if NOD_PRIMARY.match(label) and primary is None:
            primary = docid
        elif MANAGERS_ORDER_DOC.search(label) and managers is None:
            managers = docid
        elif SCHEDULE_DOC.search(label) and schedule is None:
            schedule = docid
        elif NOD_FALLBACK.search(label) and fallback is None:
            fallback = docid
        elif DECISION_DOC.match(label) and decision is None:
            decision = docid
        elif NOTIFICATION_DOC.match(label) and notification is None:
            notification = docid
        elif CORRESPONDENCE_DOC.match(label) and correspondence is None:
            correspondence = docid
    # Managers/CE Order carries the reasons schedule, so prefer it over the bare
    # "Notification" cover letter; both rank below an explicit Notification of
    # Decision / Schedule of Conditions / Decision doc.
    return (primary or schedule or fallback or decision
            or managers or notification or correspondence)


def fetch_doc_bytes(client: httpx.Client, base: str, docid: str) -> bytes:
    r1 = client.get(f"{base}/ViewFiles.aspx?docid={docid}&format=djvu")
    if r1.status_code != 200:
        return b""
    iframe = HTMLParser(r1.text).css_first("iframe")
    if not iframe or not iframe.attributes.get("src"):
        return b""
    r2 = client.get(f"{base}/{iframe.attributes['src']}")
    if r2.status_code != 200:
        return b""
    m = _FILE_REF_RE.search(r2.text)
    if not m:
        return b""
    r3 = client.get(f"{base}/files/{m.group(1)}")
    ct = r3.headers.get("content-type", "")
    if r3.status_code != 200 or not (ct.startswith("application/pdf") or "djvu" in ct):
        return b""
    return r3.content


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="acp.db")
    parser.add_argument("--council", required=True, help="planning_authority value, e.g. 'Kerry County Council'")
    parser.add_argument("--base", required=True, help="iDocs base URL, no trailing slash")
    parser.add_argument("--delay", type=float, default=0.8)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--from-date", type=str, default="2011-01-01")
    parser.add_argument("--no-followup", action="store_true")
    parser.add_argument(
        "--insecure", action="store_true",
        help="Skip TLS cert verification. Needed for councils with a broken/"
             "misconfigured cert chain (e.g. Sligo's www.sligococo.ie).",
    )
    args = parser.parse_args()

    base = args.base.rstrip("/")
    conn = open_db(args.db)

    if args.retry:
        where_extra = (
            " AND NOT EXISTS (SELECT 1 FROM council_reasons_fetch f "
            "   WHERE f.object_id = pa.object_id "
            "     AND (f.error_message IS NULL OR f.error_message = ''))"
        )
    else:
        where_extra = (
            " AND NOT EXISTS (SELECT 1 FROM council_reasons_fetch f "
            "   WHERE f.object_id = pa.object_id)"
        )

    sql = (
        "SELECT pa.object_id, pa.application_number "
        "  FROM planning_applications pa "
        " WHERE pa.planning_authority = ? "
        "   AND pa.decision LIKE '%Refuse%' "
        "   AND pa.application_number IS NOT NULL AND pa.application_number != '' "
        "   AND pa.decision_date >= ? "
        f"  {where_extra} "
        " ORDER BY pa.decision_date DESC"
    )
    if args.limit > 0:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql, (args.council, args.from_date)).fetchall()
    print(f"to fetch: {len(rows):,} {args.council} refusals", flush=True)
    if not rows:
        return 0

    n_with_reasons = n_empty = n_error = 0
    t0 = time.monotonic()

    with httpx.Client(timeout=60, follow_redirects=True,
                      verify=not args.insecure,
                      headers={"User-Agent": USER_AGENT}) as client:
        try:
            for i, r in enumerate(rows, 1):
                object_id = r["object_id"]
                ref = r["application_number"]
                error_msg: str | None = None
                reasons: list[str] = []
                try:
                    docid = fetch_nod_docid(client, base, ref)
                    if not docid:
                        error_msg = "no decision document on portal"
                    else:
                        time.sleep(args.delay)
                        doc = fetch_doc_bytes(client, base, docid)
                        if not doc:
                            error_msg = "doc fetch failed in viewer chain"
                        else:
                            txt = _extract_text(doc)
                            if not txt:
                                error_msg = "text extraction empty (incl OCR)"
                            else:
                                reasons = parse_reasons(txt)
                                if not reasons:
                                    error_msg = "no reasons section parsed"
                except httpx.HTTPError as e:
                    error_msg = str(e)[:300]
                except Exception as e:  # noqa: BLE001
                    error_msg = f"{type(e).__name__}: {e}"[:300]

                now = datetime.now(timezone.utc).isoformat()
                conn.execute("DELETE FROM council_refusal_reasons WHERE object_id = ?", (object_id,))
                for n, txt in enumerate(reasons, 1):
                    conn.execute(
                        "INSERT INTO council_refusal_reasons "
                        "(object_id, reason_number, short_prescription, raw_text, fetched_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (object_id, n, None, txt, now),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO council_reasons_fetch "
                    "(object_id, fetched_at, reasons_count, error_message) VALUES (?, ?, ?, ?)",
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
                        f"empty={n_empty:,} errors={n_error:,}  ~{rate:.2f}/s ETA {eta:.0f} min",
                        flush=True,
                    )
        finally:
            conn.commit()

    print(f"done. with_reasons={n_with_reasons:,} empty={n_empty:,} errors={n_error:,}")

    if not args.no_followup and n_with_reasons > 0:
        scripts_dir = Path(__file__).parent
        for stage in ("classify-council-reasons.py", "categorize-council-reasons.py"):
            print(f"\n=== auto: {stage} ===", flush=True)
            try:
                subprocess.run([sys.executable, str(scripts_dir / stage), "--db", args.db], check=True)
            except subprocess.CalledProcessError as e:
                print(f"  {stage} failed (exit {e.returncode}); continuing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
