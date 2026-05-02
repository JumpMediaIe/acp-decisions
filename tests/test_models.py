"""Tests for the dataclass domain models."""
from datetime import datetime, timezone

from acp_decisions.models import Decision, RefusalReason


def test_decision_minimal_construction() -> None:
    """A Decision can be constructed with only required fields."""
    d = Decision(
        reference="PL06F.249482",
        decision_date="2024-03-14",
        county_raw="Cork County Council",
        development_type_raw="House extension",
        decision_outcome="refused",
        scraped_at=datetime(2026, 5, 1, tzinfo=timezone.utc).isoformat(),
    )
    assert d.reference == "PL06F.249482"
    assert d.county is None  # mapping happens later
    assert d.refusal_reasons == []  # default empty


def test_decision_with_reasons() -> None:
    d = Decision(
        reference="PL06F.249482",
        decision_date="2024-03-14",
        county_raw="Cork County Council",
        development_type_raw="House extension",
        decision_outcome="refused",
        scraped_at="2026-05-01T00:00:00+00:00",
        refusal_reasons=[
            RefusalReason(reason_number=1, raw_text="Overbearing impact..."),
            RefusalReason(reason_number=2, raw_text="Insufficient garden..."),
        ],
    )
    assert len(d.refusal_reasons) == 2
    assert d.refusal_reasons[0].reason_number == 1


def test_refusal_reason_construction() -> None:
    r = RefusalReason(reason_number=1, raw_text="The proposed development...")
    assert r.reason_number == 1
    assert r.raw_text.startswith("The proposed")
