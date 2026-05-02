"""Tests for the ACP free-text → DevelopmentTypeId mapper."""
import pytest

from acp_decisions.devtype_map import map_devtype


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("House extension", "house_extension_rear"),
        ("Two-storey extension to rear", "house_extension_rear"),
        ("Side and rear extension", "house_extension_side"),
        ("Single dwelling", "new_house_rural"),
        ("New dwelling", "new_house_rural"),
        ("Solar PV array", "solar_panels_house"),
        ("Solar farm", "solar_farm"),
        ("Wind turbine", "wind_turbine_single"),
        ("Garage", "garage_shed_outbuilding"),
        ("Shed", "garage_shed_outbuilding"),
        ("Demolition and replacement of dwelling", "demolition_replacement_dwelling"),
    ],
)
def test_map_devtype_known(raw: str, expected: str) -> None:
    assert map_devtype(raw) == expected


def test_map_devtype_unknown_returns_none() -> None:
    assert map_devtype("Bizarre development that doesn't match anything") is None


def test_map_devtype_handles_long_descriptions() -> None:
    raw = (
        "Construction of a single-storey extension to the rear of an existing dwelling, "
        "ancillary site works, and connection to public services."
    )
    assert map_devtype(raw) == "house_extension_rear"
