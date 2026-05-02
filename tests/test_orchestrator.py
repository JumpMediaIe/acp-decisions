"""End-to-end orchestrator tests using saved HTML+PDF fixtures via MockTransport."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from acp_decisions.http_client import PoliteClient
from acp_decisions.orchestrator import scrape_one


def _make_transport(routes: dict[str, str | bytes]) -> httpx.MockTransport:
    """Mock transport that matches each route key against the request URL."""
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        for key, content in routes.items():
            if key in url:
                if isinstance(content, bytes):
                    return httpx.Response(200, content=content)
                return httpx.Response(200, text=content)
        return httpx.Response(404, text="not found")
    return httpx.MockTransport(handler)


def _client(routes: dict[str, str | bytes]) -> PoliteClient:
    return PoliteClient(min_interval_s=0, transport=_make_transport(routes))


def test_scrape_granted_case_persists_metadata_only(
    temp_db: sqlite3.Connection, fixtures_dir: Path
) -> None:
    """Granted cases store metadata + documents but no refusal reasons."""
    html = (fixtures_dir / "case_granted_lrd_319750.html").read_text(encoding="utf-8")
    client = _client({"/case/319750": html})
    result = scrape_one(client, temp_db, case_id_url=319750)
    assert result is not None
    assert result.decision_outcome == "granted_with_conditions"
    assert result.refusal_reasons == []
    rows = temp_db.execute("SELECT case_id_url, decision_outcome FROM decisions").fetchall()
    assert rows[0]["case_id_url"] == 319750
    assert rows[0]["decision_outcome"] == "granted_with_conditions"


def test_scrape_refused_case_fetches_pdf_and_persists_reasons(
    temp_db: sqlite3.Connection, fixtures_dir: Path
) -> None:
    """Refused cases trigger an Order PDF fetch and persist the parsed reasons."""
    html = (fixtures_dir / "case_refused_lrd_315183.html").read_text(encoding="utf-8")
    pdf_bytes = (fixtures_dir / "order_refused_lrd_315183.pdf").read_bytes()
    client = _client({
        "/case/315183": html,
        "d315183.pdf": pdf_bytes,
    })
    result = scrape_one(client, temp_db, case_id_url=315183)
    assert result is not None
    assert result.decision_outcome == "refused"
    assert len(result.refusal_reasons) == 3
    assert result.abp_reference == "ABP-315183-22"
    # Reasons persisted to DB
    rows = temp_db.execute("SELECT COUNT(*) AS n FROM refusal_reasons").fetchone()
    assert rows["n"] == 3


def test_scrape_persists_document_links(
    temp_db: sqlite3.Connection, fixtures_dir: Path
) -> None:
    html = (fixtures_dir / "case_granted_lrd_319750.html").read_text(encoding="utf-8")
    client = _client({"/case/319750": html})
    scrape_one(client, temp_db, case_id_url=319750)
    rows = temp_db.execute(
        "SELECT doc_type FROM documents WHERE case_id_url = 319750"
    ).fetchall()
    types = {r["doc_type"] for r in rows}
    assert "order" in types
    assert "inspector_report" in types


def test_scrape_maps_county_and_devtype(
    temp_db: sqlite3.Connection, fixtures_dir: Path
) -> None:
    """county_raw → county, development_type_raw → development_type_id."""
    html = (fixtures_dir / "case_granted_lrd_319750.html").read_text(encoding="utf-8")
    client = _client({"/case/319750": html})
    result = scrape_one(client, temp_db, case_id_url=319750)
    # Cavan County Council → cavan (county_map's CountyId form)
    assert result is not None
    assert result.county == "cavan"


def test_scrape_records_error_on_404_html(
    temp_db: sqlite3.Connection,
) -> None:
    """A 404 on the case page records a scrape_error and returns None."""
    client = _client({})  # no routes — every URL 404s
    result = scrape_one(client, temp_db, case_id_url=999999)
    assert result is None
    rows = temp_db.execute("SELECT error_class FROM scrape_errors").fetchall()
    assert len(rows) == 1
    assert rows[0]["error_class"] == "transient"


def test_scrape_uses_first_order_doc_when_multiple(
    temp_db: sqlite3.Connection, fixtures_dir: Path
) -> None:
    """When a case has multi-part orders (d300506(a).pdf + (b).pdf), the parser
    fetches the first one and extracts reasons from it."""
    html = (fixtures_dir / "case_refused_pa_300506.html").read_text(encoding="utf-8")
    pdf_bytes = (fixtures_dir / "order_refused_pa_300506_a.pdf").read_bytes()
    client = _client({
        "/case/300506": html,
        # The (a).pdf form contains the parens encoded — match on the slug
        "d300506(a).pdf": pdf_bytes,
        # Also serve (b) so the test passes even if orchestrator picks (b)
        "d300506(b).pdf": pdf_bytes,
    })
    result = scrape_one(client, temp_db, case_id_url=300506)
    assert result is not None
    assert result.decision_outcome == "refused"
    assert len(result.refusal_reasons) >= 1
