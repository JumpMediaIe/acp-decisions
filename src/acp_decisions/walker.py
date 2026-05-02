"""Walk ACP type-filtered case listings to discover case IDs.

The cases listing at `https://www.pleanala.ie/en-ie/cases?type={CODE}` is
server-rendered with all `/en-ie/case/{id}` anchor links inline. A single GET
returns every case under that type filter (no pagination observed for current
type filters as of 2026-05).

If a future ACP redesign adds pagination, extend this module — the orchestrator
deals only in the case-ID iterator surface.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from acp_decisions.http_client import PoliteClient

_CASE_ID_RE = re.compile(r"/en-ie/case/(\d+)")
_BASE_LISTING_URL = "https://www.pleanala.ie/en-ie/cases"


def parse_listing(html: str) -> list[int]:
    """Return unique case IDs found in a listing page, in source order."""
    seen: set[int] = set()
    ids: list[int] = []
    for m in _CASE_ID_RE.finditer(html):
        cid = int(m.group(1))
        if cid not in seen:
            seen.add(cid)
            ids.append(cid)
    return ids


def fetch_case_ids_for_type(client: PoliteClient, type_code: str) -> list[int]:
    """Fetch one type-filtered listing page; return its unique case IDs."""
    url = f"{_BASE_LISTING_URL}?type={type_code}"
    html = client.get(url)
    return parse_listing(html)


def fetch_all_case_ids(client: PoliteClient, type_codes: Iterable[str]) -> list[int]:
    """Walk multiple type filters; return the union of case IDs across all of them."""
    seen: set[int] = set()
    out: list[int] = []
    for code in type_codes:
        for cid in fetch_case_ids_for_type(client, code):
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
    return out
