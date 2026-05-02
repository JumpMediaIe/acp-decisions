"""Tests for the ACP Order PDF parser, run against saved fixture PDFs."""
from __future__ import annotations

from pathlib import Path

import pytest

from acp_decisions.pdf_parser import parse_order_pdf


@pytest.fixture
def short_order_bytes(fixtures_dir: Path) -> bytes:
    return (fixtures_dir / "order_refused_lrd_315183.pdf").read_bytes()


@pytest.fixture
def long_order_bytes(fixtures_dir: Path) -> bytes:
    return (fixtures_dir / "order_refused_pa_300506_a.pdf").read_bytes()


def test_short_order_extracts_abp_reference(short_order_bytes: bytes) -> None:
    result = parse_order_pdf(short_order_bytes)
    assert result.abp_reference == "ABP-315183-22"


def test_short_order_extracts_three_reasons(short_order_bytes: bytes) -> None:
    """Short-form Order: clean numbered list right after the header."""
    result = parse_order_pdf(short_order_bytes)
    assert len(result.reasons) == 3
    assert [r.reason_number for r in result.reasons] == [1, 2, 3]


def test_short_order_reason_text_substantive(short_order_bytes: bytes) -> None:
    result = parse_order_pdf(short_order_bytes)
    r1 = result.reasons[0]
    assert "29 zoning" in r1.raw_text
    assert "proper planning and sustainable development" in r1.raw_text
    # Reasons should be substantial — multi-paragraph text
    assert len(r1.raw_text) > 200


def test_short_order_handles_missing_period_in_third_reason(short_order_bytes: bytes) -> None:
    """In this fixture, reason 3 starts with '3 Having' (no period)."""
    result = parse_order_pdf(short_order_bytes)
    r3 = result.reasons[2]
    assert "Natura Impact Statement" in r3.raw_text


def test_long_order_skips_legislative_preamble(long_order_bytes: bytes) -> None:
    """Long-form Order: 'Reasons and Considerations' header appears twice. The first
    is the legislative preamble (bullet list, no numbered reasons). The parser must
    anchor on the second occurrence which is followed by numbered items."""
    result = parse_order_pdf(long_order_bytes)
    # 4 numbered reasons in this fixture
    assert len(result.reasons) == 4
    assert [r.reason_number for r in result.reasons] == [1, 2, 3, 4]


def test_long_order_extracts_abp_reference(long_order_bytes: bytes) -> None:
    result = parse_order_pdf(long_order_bytes)
    assert result.abp_reference is not None
    assert result.abp_reference.startswith("ABP-300506")


def test_short_order_strips_page_footers(short_order_bytes: bytes) -> None:
    """Reason text should not contain interleaved page-footer noise."""
    result = parse_order_pdf(short_order_bytes)
    for r in result.reasons:
        assert "Page" not in r.raw_text or "of 5" not in r.raw_text
        assert "ABP-315183" not in r.raw_text


def test_decision_verb_extracted(short_order_bytes: bytes) -> None:
    result = parse_order_pdf(short_order_bytes)
    assert result.decision_verb == "REFUSE"
