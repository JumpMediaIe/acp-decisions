"""Compose the scraper pipeline: case page + Order PDF → DB.

For one case ID:

    1. GET the case page HTML
    2. Parse metadata + document links
    3. Normalise decision outcome → canonical bucket
    4. Map free-text county and dev-type to canonical IDs
    5. If the outcome is 'refused', GET the first Order PDF and parse
       refusal reasons + ABP reference
    6. Upsert the decision, its documents, and (if any) its reasons

Errors are caught and recorded in the `scrape_errors` table; the orchestrator
returns None for the failed case so the caller can keep going.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from acp_decisions.county_map import map_county
from acp_decisions.devtype_map import map_devtype
from acp_decisions.http_client import PoliteClient, ScraperError
from acp_decisions.models import Decision, DocumentLink, ScrapeError as ScrapeErrorRow
from acp_decisions.outcome import normalise_outcome
from acp_decisions.parser import parse_case_page
from acp_decisions.pdf_parser import parse_order_pdf
from acp_decisions.upsert import (
    record_scrape_error,
    upsert_decision,
    upsert_documents,
    upsert_reasons,
)


_CASE_URL_TPL = "https://www.pleanala.ie/en-ie/case/{case_id}"


def scrape_one(
    client: PoliteClient,
    conn: sqlite3.Connection,
    case_id_url: int,
) -> Decision | None:
    """Scrape one case end-to-end. Returns the persisted Decision, or None on failure."""
    now = _now_iso()
    url = _CASE_URL_TPL.format(case_id=case_id_url)

    try:
        html = client.get(url)
    except ScraperError as e:
        record_scrape_error(
            conn,
            ScrapeErrorRow(
                error_class="transient",
                occurred_at=now,
                case_id_url=case_id_url,
                message=str(e),
            ),
        )
        return None

    try:
        decision, documents = parse_case_page(html, case_id_url=case_id_url, scraped_at=now)
    except Exception as e:  # noqa: BLE001 — anything unexpected from selectolax/regex
        record_scrape_error(
            conn,
            ScrapeErrorRow(
                error_class="parse_error",
                occurred_at=now,
                case_id_url=case_id_url,
                message=str(e),
            ),
        )
        return None

    decision.decision_outcome = normalise_outcome(decision.decision_outcome_raw)
    decision.county = map_county(decision.county_raw)
    decision.development_type_id = map_devtype(decision.development_type_raw)

    if decision.decision_outcome == "refused":
        _attach_refusal_reasons(client, conn, decision, documents, now)

    upsert_decision(conn, decision)
    upsert_documents(conn, decision.case_id_url, documents)
    if decision.refusal_reasons:
        upsert_reasons(conn, decision.case_id_url, decision.refusal_reasons)
    return decision


def _attach_refusal_reasons(
    client: PoliteClient,
    conn: sqlite3.Connection,
    decision: Decision,
    documents: list[DocumentLink],
    now: str,
) -> None:
    """Find the Order PDF, fetch it, parse reasons and ABP ref onto the Decision."""
    order = next((d for d in documents if d.doc_type == "order"), None)
    if order is None:
        return
    try:
        pdf_bytes = client.get_bytes(order.url)
        result = parse_order_pdf(pdf_bytes)
    except Exception as e:  # noqa: BLE001
        record_scrape_error(
            conn,
            ScrapeErrorRow(
                error_class="parse_error",
                occurred_at=now,
                case_id_url=decision.case_id_url,
                message=f"order pdf: {e}",
            ),
        )
        return
    decision.refusal_reasons = result.reasons
    if result.abp_reference is not None:
        decision.abp_reference = result.abp_reference


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
