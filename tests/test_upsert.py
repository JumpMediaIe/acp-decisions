"""Tests for the DB upsert layer."""
from __future__ import annotations

import sqlite3

from acp_decisions.models import Decision, DocumentLink, RefusalReason, ScrapeError
from acp_decisions.upsert import (
    record_scrape_error,
    upsert_decision,
    upsert_documents,
    upsert_reasons,
)


def _make_decision(case_id: int = 315183, **overrides: object) -> Decision:
    base = dict(
        case_id_url=case_id,
        decision_date="2024-09-05",
        county_raw="Cavan County Council",
        development_type_raw="House extension",
        decision_outcome="refused",
        decision_outcome_raw="Refuse Permission",
        scraped_at="2026-05-02T22:00:00+00:00",
    )
    base.update(overrides)  # type: ignore[arg-type]
    return Decision(**base)  # type: ignore[arg-type]


def test_upsert_decision_inserts_new(temp_db: sqlite3.Connection) -> None:
    upsert_decision(temp_db, _make_decision())
    rows = temp_db.execute("SELECT case_id_url, decision_outcome FROM decisions").fetchall()
    assert len(rows) == 1
    assert rows[0]["case_id_url"] == 315183


def test_upsert_decision_updates_on_conflict(temp_db: sqlite3.Connection) -> None:
    """A second upsert with the same case_id_url updates rather than duplicating."""
    upsert_decision(temp_db, _make_decision(decision_outcome="refused"))
    upsert_decision(temp_db, _make_decision(decision_outcome="granted"))
    rows = temp_db.execute("SELECT decision_outcome FROM decisions").fetchall()
    assert len(rows) == 1
    assert rows[0]["decision_outcome"] == "granted"


def test_upsert_decision_persists_optional_fields(temp_db: sqlite3.Connection) -> None:
    d = _make_decision(
        abp_reference="ABP-315183-22",
        pa_reference="LH02.315183",
        site_address="Drumlark Townland",
        county="cavan_county",
        development_type_id="house_extension_rear",
        case_type_raw="Appeal - LRD",
        applicant_name_raw="Some Ltd.",
    )
    upsert_decision(temp_db, d)
    row = temp_db.execute(
        "SELECT abp_reference, pa_reference, site_address, county, "
        "development_type_id, case_type_raw, applicant_name_raw FROM decisions"
    ).fetchone()
    assert row["abp_reference"] == "ABP-315183-22"
    assert row["pa_reference"] == "LH02.315183"
    assert row["site_address"] == "Drumlark Townland"
    assert row["county"] == "cavan_county"
    assert row["development_type_id"] == "house_extension_rear"
    assert row["case_type_raw"] == "Appeal - LRD"
    assert row["applicant_name_raw"] == "Some Ltd."


def test_upsert_documents_inserts(temp_db: sqlite3.Connection) -> None:
    upsert_decision(temp_db, _make_decision())
    docs = [
        DocumentLink(doc_type="order", url="https://example.com/o.pdf"),
        DocumentLink(doc_type="inspector_report", url="https://example.com/r.pdf"),
    ]
    upsert_documents(temp_db, case_id_url=315183, documents=docs)
    rows = temp_db.execute(
        "SELECT doc_type, url FROM documents ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["doc_type"] == "order"


def test_upsert_documents_replaces_existing(temp_db: sqlite3.Connection) -> None:
    upsert_decision(temp_db, _make_decision())
    upsert_documents(temp_db, 315183, [DocumentLink(doc_type="order", url="https://e.com/old.pdf")])
    upsert_documents(temp_db, 315183, [DocumentLink(doc_type="order", url="https://e.com/new.pdf")])
    rows = temp_db.execute("SELECT url FROM documents").fetchall()
    assert len(rows) == 1
    assert rows[0]["url"] == "https://e.com/new.pdf"


def test_upsert_reasons_returns_inserted_ids(temp_db: sqlite3.Connection) -> None:
    upsert_decision(temp_db, _make_decision())
    reasons = [
        RefusalReason(reason_number=1, raw_text="First reason..."),
        RefusalReason(reason_number=2, raw_text="Second reason..."),
    ]
    ids = upsert_reasons(temp_db, 315183, reasons)
    assert len(ids) == 2
    assert all(isinstance(i, int) for i in ids)


def test_upsert_reasons_replaces_on_repeat(temp_db: sqlite3.Connection) -> None:
    """A second classification run shouldn't duplicate refusal reasons."""
    upsert_decision(temp_db, _make_decision())
    upsert_reasons(temp_db, 315183, [RefusalReason(reason_number=1, raw_text="First v1")])
    upsert_reasons(temp_db, 315183, [RefusalReason(reason_number=1, raw_text="First v2")])
    rows = temp_db.execute("SELECT raw_text FROM refusal_reasons").fetchall()
    assert len(rows) == 1
    assert rows[0]["raw_text"] == "First v2"


def test_record_scrape_error(temp_db: sqlite3.Connection) -> None:
    err = ScrapeError(
        error_class="parse_error",
        occurred_at="2026-05-02T22:00:00+00:00",
        case_id_url=999999,
        message="couldn't find decision date",
    )
    record_scrape_error(temp_db, err)
    rows = temp_db.execute(
        "SELECT case_id_url, error_class, message FROM scrape_errors"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["error_class"] == "parse_error"
    assert rows[0]["case_id_url"] == 999999


def test_documents_cascade_on_decision_delete(temp_db: sqlite3.Connection) -> None:
    """FK constraint: deleting a decision cascades to its documents."""
    upsert_decision(temp_db, _make_decision())
    upsert_documents(temp_db, 315183, [DocumentLink(doc_type="order", url="x")])
    temp_db.execute("DELETE FROM decisions WHERE case_id_url = 315183")
    rows = temp_db.execute("SELECT 1 FROM documents").fetchall()
    assert rows == []
