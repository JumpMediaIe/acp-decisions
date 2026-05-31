"""Fetch refusal reasons from Monaghan County Council's planning portal.

Monaghan is NOT on iDocs. It runs Laserfiche **WebLink 11** (an Angular SPA at
https://portal.monaghancoco.ie/WebLinkPlanningPortal), so it needs its own
fetch path. The reason-parsing itself is shared: we import `parse_reasons` from
fetch-idocs-reasons.py, because Monaghan's decision wording ("...for the N
reason(s) set out on the Schedule attached" then numbered items) matches the
same markers.

WebLink 11 flow (all discovered by reading app/dist/search/main.js):
    1. GET search.aspx?dbid=0&cr=1 with browser-like headers -> sets the
       `lastSessionAccess` cookie. WITHOUT a real Accept/UA the server 403s.
    2. POST DocumentService.aspx/GetRepoNameByDbid {dbid:0} -> repo name
       (MONLFPLANNING). Done once and cached.
    3. POST SearchService.aspx/GetSearchListing with searchSyn
       '{LF:Name~="<ref>"}' -> a list of result rows. Each planning document is
       a SEPARATE entry named "<ref> - <Document Type>" with an `entryId`.
    4. Pick the decision-bearing doc by name: "Chief Executives Order" carries
       the reasons schedule (primary); "Notification of Decision" is a fallback.
    5. POST DocumentService.aspx/GetTextHtmlForPage for each page (1..pageCount)
       -> server-side extracted text as HTML. NO local OCR needed; Monaghan's
       docs are native PDFs with a clean text layer that the server exposes.
    6. Strip the HTML, join the pages, hand to parse_reasons().

Mirrors fetch-idocs-reasons.py for DB writes, --retry semantics, progress
logging, and the auto classify+categorise follow-up.

Usage:
    uv run python scripts/fetch-monaghan-reasons.py --db acp.db [--retry]
        [--delay 0.6] [--limit N] [--from-date 2011-01-01] [--no-followup]
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from acp_decisions.db import open_db

# Reuse the shared reason parser (markers cover Monaghan's wording). The iDocs
# scraper's filename is hyphenated, so load it by path rather than import.
_idocs_spec = importlib.util.spec_from_file_location(
    "fetch_idocs_reasons", str(Path(__file__).with_name("fetch-idocs-reasons.py"))
)
_idocs = importlib.util.module_from_spec(_idocs_spec)
_idocs_spec.loader.exec_module(_idocs)
parse_reasons = _idocs.parse_reasons


COUNCIL = "Monaghan County Council"
BASE = "https://portal.monaghancoco.ie/WebLinkPlanningPortal"
DBID = 0

# A real browser UA + Accept is required — the portal 403s plain clients.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CONTACT_UA = (
    "planningcheck.ie scraper - public records reproduction "
    "(contact: contact@planningcheck.ie)"
)

# Decision-document name priority. Monaghan files each document as a separate
# entry named "<ref> - <Document Type>"; the reasons schedule is appended to the
# Chief Executive's Order. Some refs only have a scanned Order with no server
# text layer but DO carry a "Chief Executives AI Order" (Additional Information
# order) that holds the operative reasons, and the Notification of Decision can
# also carry them. So we build a PRIORITY-ORDERED candidate list and try each
# until one yields parseable reasons.
#   1. plain "Chief Executive's Order"        (usual reasons schedule)
#   2. "Chief Executive's AI Order"           (additional-information order)
#   3. "Notification of Decision"             (cover letter; sometimes lists them)
_DOC_PRIORITY = (
    re.compile(r"chief\s+executive'?s?\s+order", re.I),
    re.compile(r"chief\s+executive'?s?\s+ai\s+order", re.I),
    re.compile(r"notification\s+of\s+decision", re.I),
)

_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(s: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", s or ""))


def prime_session(client: httpx.Client) -> None:
    """Hit the SPA search page so the server issues the session cookie."""
    client.get(
        f"{BASE}/search.aspx?dbid={DBID}&cr=1",
        headers={"Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-IE,en;q=0.9"},
    )


def _api_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE}/search.aspx?dbid={DBID}&cr=1",
    }


def get_repo_name(client: httpx.Client) -> str:
    r = client.post(
        f"{BASE}/DocumentService.aspx/GetRepoNameByDbid",
        headers=_api_headers(),
        json={"dbid": DBID},
    )
    r.raise_for_status()
    return r.json()["data"]


def search_entries(client: httpx.Client, repo: str, ref: str) -> list[dict]:
    """Return the result rows for an exact reference-name search."""
    r = client.post(
        f"{BASE}/SearchService.aspx/GetSearchListing",
        headers=_api_headers(),
        json={
            "repoName": repo,
            "searchSyn": f'{{LF:Name~="{ref}"}}',
            "searchUuid": "",
            "sortColumn": "",
            "startIdx": 1,
            "endIdx": 100,
            "getNewListing": True,
            "sortOrder": 0,
            "displayInGridView": False,
        },
    )
    r.raise_for_status()
    data = r.json().get("data") or {}
    if data.get("failed"):
        return []
    return data.get("results") or []


def pick_decision_entries(entries: list[dict], ref: str) -> list[tuple[int, int]]:
    """Decision-bearing entries for `ref`, in priority order: (entryId, pages).

    The result set also contains other applications whose reference is a
    superstring/substring of `ref` (Name~= is a contains match), so we require
    the doc name to start with the exact ref before the ' - <type>' suffix.

    Returns possibly-several candidates (one per matched priority tier), because
    a higher-priority doc can be a scanned PDF with no server text layer while a
    lower-priority one carries the reasons. The caller tries them in order.
    """
    matched: dict[int, tuple[int, int]] = {}
    for e in entries:
        name = (e.get("name") or "").strip()
        # Names look like "<ref> - <Document Type>". Guard the ref boundary.
        if name.split(" - ", 1)[0].strip() != ref:
            continue
        pages = e.get("thumbnailPageCount") or 0
        eid = e.get("entryId")
        if eid is None or pages <= 0:
            continue
        # AI Order also matches the plain-Order regex (tier 0) via "...Order",
        # so test the AI pattern first to assign the right, more specific tier.
        if _DOC_PRIORITY[1].search(name):
            tier = 1
        elif _DOC_PRIORITY[0].search(name):
            tier = 0
        elif _DOC_PRIORITY[2].search(name):
            tier = 2
        else:
            continue
        matched.setdefault(tier, (int(eid), int(pages)))
    return [matched[t] for t in sorted(matched)]


def fetch_entry_text(client: httpx.Client, repo: str, entry_id: int, pages: int) -> str:
    out: list[str] = []
    for p in range(1, pages + 1):
        r = client.post(
            f"{BASE}/DocumentService.aspx/GetTextHtmlForPage",
            headers=_api_headers(),
            json={
                "repoName": repo,
                "documentId": entry_id,
                "pageNum": p,
                "showAnn": False,
                "searchUuid": "",
            },
        )
        if r.status_code != 200:
            continue
        data = r.json().get("data")
        if isinstance(data, dict):
            data = data.get("text") or data.get("html") or ""
        out.append(_html_to_text(str(data)))
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="acp.db")
    parser.add_argument("--delay", type=float, default=0.6)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--from-date", type=str, default="2011-01-01")
    parser.add_argument("--no-followup", action="store_true")
    args = parser.parse_args()

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
    rows = conn.execute(sql, (COUNCIL, args.from_date)).fetchall()
    print(f"to fetch: {len(rows):,} {COUNCIL} refusals", flush=True)
    if not rows:
        return 0

    n_with_reasons = n_empty = n_error = 0
    t0 = time.monotonic()

    with httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": UA}) as client:
        try:
            prime_session(client)
            repo = get_repo_name(client)
            print(f"repo: {repo}", flush=True)

            for i, r in enumerate(rows, 1):
                object_id = r["object_id"]
                ref = r["application_number"]
                error_msg: str | None = None
                reasons: list[str] = []
                try:
                    entries = search_entries(client, repo, ref)
                    if not entries:
                        error_msg = "no search results on portal"
                    else:
                        candidates = pick_decision_entries(entries, ref)
                        if not candidates:
                            error_msg = "no decision document on portal"
                        else:
                            # Try each candidate in priority order; a higher-
                            # priority doc may be a scanned PDF with no server
                            # text, while a lower one carries the reasons.
                            error_msg = "no reasons section parsed"
                            for entry_id, pages in candidates:
                                time.sleep(args.delay)
                                txt = fetch_entry_text(client, repo, entry_id, pages)
                                if not txt.strip():
                                    continue
                                found = parse_reasons(txt)
                                if found:
                                    reasons = found
                                    error_msg = None
                                    break
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
