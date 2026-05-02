"""Tests for the ACP case-page HTML metadata parser, run against saved fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest

from acp_decisions.parser import parse_case_page


@pytest.fixture
def granted_lrd(fixtures_dir: Path) -> str:
    return (fixtures_dir / "case_granted_lrd_319750.html").read_text(encoding="utf-8")


@pytest.fixture
def refused_lrd(fixtures_dir: Path) -> str:
    return (fixtures_dir / "case_refused_lrd_315183.html").read_text(encoding="utf-8")


@pytest.fixture
def refused_pa(fixtures_dir: Path) -> str:
    return (fixtures_dir / "case_refused_pa_300506.html").read_text(encoding="utf-8")


SCRAPED_AT = "2026-05-02T22:00:00+00:00"


def test_parses_case_id_url_passthrough(granted_lrd: str) -> None:
    d, _docs = parse_case_page(granted_lrd, case_id_url=319750, scraped_at=SCRAPED_AT)
    assert d.case_id_url == 319750


def test_parses_pa_reference_from_section_title(granted_lrd: str) -> None:
    d, _docs = parse_case_page(granted_lrd, case_id_url=319750, scraped_at=SCRAPED_AT)
    assert d.pa_reference == "LH02.319750"


def test_parses_pa_reference_for_pa_format(refused_pa: str) -> None:
    d, _docs = parse_case_page(refused_pa, case_id_url=300506, scraped_at=SCRAPED_AT)
    assert d.pa_reference == "PA09.300506"


def test_parses_council_as_county_raw(granted_lrd: str) -> None:
    d, _docs = parse_case_page(granted_lrd, case_id_url=319750, scraped_at=SCRAPED_AT)
    assert d.county_raw == "Cavan County Council"


def test_parses_address_into_site_address(granted_lrd: str) -> None:
    d, _docs = parse_case_page(granted_lrd, case_id_url=319750, scraped_at=SCRAPED_AT)
    assert d.site_address is not None
    assert "Drumlark" in d.site_address


def test_parses_description_as_development_type_raw(granted_lrd: str) -> None:
    d, _docs = parse_case_page(granted_lrd, case_id_url=319750, scraped_at=SCRAPED_AT)
    assert "145 large scale development units" in d.development_type_raw


def test_parses_case_type_raw(granted_lrd: str) -> None:
    d, _docs = parse_case_page(granted_lrd, case_id_url=319750, scraped_at=SCRAPED_AT)
    assert d.case_type_raw == "Appeal - LRD"


def test_parses_decision_outcome_raw(granted_lrd: str) -> None:
    d, _docs = parse_case_page(granted_lrd, case_id_url=319750, scraped_at=SCRAPED_AT)
    assert d.decision_outcome_raw == "Grant permission with revised conditions"
    # HTML parser leaves decision_outcome same as raw; normaliser runs later
    assert d.decision_outcome == d.decision_outcome_raw


def test_parses_decision_date_to_iso(granted_lrd: str) -> None:
    d, _docs = parse_case_page(granted_lrd, case_id_url=319750, scraped_at=SCRAPED_AT)
    # Date signed was "05/09/2024" DD/MM/YYYY
    assert d.decision_date == "2024-09-05"


def test_parses_applicant_name_from_parties(refused_pa: str) -> None:
    d, _docs = parse_case_page(refused_pa, case_id_url=300506, scraped_at=SCRAPED_AT)
    assert d.applicant_name_raw is not None
    assert "Bord Na Mona" in d.applicant_name_raw


def test_parses_document_links_with_classification(refused_pa: str) -> None:
    _d, docs = parse_case_page(refused_pa, case_id_url=300506, scraped_at=SCRAPED_AT)
    types = {doc.doc_type for doc in docs}
    assert "order" in types
    assert "inspector_report" in types
    assert "direction" in types
    # Letter falls into 'other'
    assert "other" in types
    # All URLs absolute
    for doc in docs:
        assert doc.url.startswith("https://www.pleanala.ie/")


def test_documents_include_multi_part_orders(refused_pa: str) -> None:
    """Case 300506 has d300506(a).pdf AND d300506(b).pdf — both are orders."""
    _d, docs = parse_case_page(refused_pa, case_id_url=300506, scraped_at=SCRAPED_AT)
    orders = [doc for doc in docs if doc.doc_type == "order"]
    assert len(orders) >= 2


def test_scraped_at_passthrough(granted_lrd: str) -> None:
    d, _docs = parse_case_page(granted_lrd, case_id_url=319750, scraped_at=SCRAPED_AT)
    assert d.scraped_at == SCRAPED_AT


def test_refused_lrd_decision_outcome_raw(refused_lrd: str) -> None:
    d, _docs = parse_case_page(refused_lrd, case_id_url=315183, scraped_at=SCRAPED_AT)
    assert d.decision_outcome_raw == "Refuse Permission"
