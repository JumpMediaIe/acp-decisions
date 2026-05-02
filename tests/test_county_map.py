"""Tests for the ACP-county-name → CountyId mapper."""
import pytest

from acp_decisions.county_map import map_county


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Cork County Council", "cork_county"),
        ("Cork City Council", "cork_city"),
        ("Dublin City Council", "dublin_city"),
        ("South Dublin County Council", "south_dublin"),
        ("Dún Laoghaire-Rathdown County Council", "dun_laoghaire_rathdown"),
        ("Dun Laoghaire-Rathdown County Council", "dun_laoghaire_rathdown"),  # ASCII variant
        ("Fingal County Council", "fingal"),
        ("Galway City Council", "galway_city"),
        ("Galway County Council", "galway_county"),
        ("Kerry County Council", "kerry"),
        ("Limerick City and County Council", "limerick"),
        ("Waterford City and County Council", "waterford"),
    ],
)
def test_map_county_known(raw: str, expected: str) -> None:
    assert map_county(raw) == expected


def test_map_county_unknown_returns_none() -> None:
    assert map_county("Some Council That Doesn't Exist") is None


def test_map_county_strips_and_normalises_whitespace() -> None:
    assert map_county("  Cork County Council  ") == "cork_county"
    assert map_county("Cork  County  Council") == "cork_county"
