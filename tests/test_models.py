"""Tests for the dataclass domain models."""
from datetime import datetime, timezone

from acp_decisions.models import Decision, DocumentLink, RefusalReason


def test_decision_minimal_construction() -> None:
    """A Decision can be constructed with only required fields."""
    d = Decision(
        case_id_url=315183,
        decision_date="2024-03-14",
        county_raw="Cork County Council",
        development_type_raw="House extension",
        decision_outcome="refused",
        decision_outcome_raw="Refuse Permission",
        scraped_at=datetime(2026, 5, 1, tzinfo=timezone.utc).isoformat(),
    )
    assert d.case_id_url == 315183
    assert d.abp_reference is None
    assert d.pa_reference is None
    assert d.county is None  # mapping happens later
    assert d.refusal_reasons == []  # default empty
    assert d.documents == []


def test_decision_with_reasons_and_documents() -> None:
    d = Decision(
        case_id_url=315183,
        decision_date="2024-03-14",
        county_raw="Cork County Council",
        development_type_raw="House extension",
        decision_outcome="refused",
        decision_outcome_raw="Refuse Permission",
        scraped_at="2026-05-01T00:00:00+00:00",
        abp_reference="ABP-315183-22",
        pa_reference="LH02.315183",
        refusal_reasons=[
            RefusalReason(reason_number=1, raw_text="Overbearing impact..."),
            RefusalReason(reason_number=2, raw_text="Insufficient garden..."),
        ],
        documents=[
            DocumentLink(doc_type="order", url="https://www.pleanala.ie/foo/d315183.pdf"),
        ],
    )
    assert len(d.refusal_reasons) == 2
    assert d.refusal_reasons[0].reason_number == 1
    assert d.documents[0].doc_type == "order"


def test_refusal_reason_construction() -> None:
    r = RefusalReason(reason_number=1, raw_text="The proposed development...")
    assert r.reason_number == 1
    assert r.raw_text.startswith("The proposed")


def test_document_link_construction() -> None:
    doc = DocumentLink(doc_type="order", url="https://example.com/o.pdf")
    assert doc.doc_type == "order"
    assert doc.fetched_at is None
