"""Maps the free-text "Type of development" field on ACP decisions to a
DevelopmentTypeId matching planningcheck.ie's lib/devTypes.ts.

The mapping is heuristic — ordered list of (regex pattern, devtype id) pairs.
First match wins. Patterns are deliberately broad; precision improves over
time as we observe unmapped raw values in the database.

Returns None when no pattern matches. The scraper persists the raw text
regardless so we can audit unmapped rows and add patterns later.
"""
from __future__ import annotations

import re

# Order matters — more specific patterns first.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Extensions
    (re.compile(r"\bside\s+(?:and\s+rear|extension)", re.I),  "house_extension_side"),
    (re.compile(r"\b(?:rear|two[-\s]storey|single[-\s]storey)\s+extension", re.I), "house_extension_rear"),
    (re.compile(r"\bextension\s+to\s+(?:dwelling|house|the\s+rear)", re.I), "house_extension_rear"),
    (re.compile(r"\bextension\b", re.I), "house_extension_rear"),

    # New dwellings
    (re.compile(r"\bdemolition\s+and\s+replacement\s+(?:of\s+)?dwelling", re.I), "demolition_replacement_dwelling"),
    (re.compile(r"\b(?:single\s+|new\s+)?dwelling\b(?!\s+extension)", re.I), "new_house_rural"),
    (re.compile(r"\bnew\s+house\b", re.I), "new_house_rural"),

    # Renewable energy
    (re.compile(r"\bsolar\s+farm\b", re.I), "solar_farm"),
    (re.compile(r"\bsolar\s+(?:panels|pv|photovoltaic|array)", re.I), "solar_panels_house"),
    (re.compile(r"\bwind\s+(?:farm|turbine\s+(?:array|cluster))", re.I), "wind_farm"),
    (re.compile(r"\bwind\s+turbine\b", re.I), "wind_turbine_single"),
    (re.compile(r"\bheat\s+pump\b", re.I), "heat_pump_air"),
    (re.compile(r"\bbattery\s+(?:storage|energy)", re.I), "battery_storage"),

    # Outbuildings
    (re.compile(r"\b(?:garage|carport)\b", re.I), "garage_shed_outbuilding"),
    (re.compile(r"\b(?:garden\s+room|shed|workshop|outbuilding|store(?:\s+building)?)\b", re.I), "garage_shed_outbuilding"),

    # Other common categories
    (re.compile(r"\bporch\b", re.I), "porch"),
    (re.compile(r"\battic\s+conversion\b", re.I), "attic_conversion"),
    (re.compile(r"\bdormer\b", re.I), "attic_conversion"),
    (re.compile(r"\b(?:driveway|paving)\b", re.I), "driveway"),
    (re.compile(r"\bagricultural\s+(?:building|shed)", re.I), "agricultural_building"),
    (re.compile(r"\bchange\s+of\s+use\b", re.I), "change_of_use_general"),
]


def map_devtype(raw: str) -> str | None:
    """Return the DevelopmentTypeId for an ACP "Type of development" string,
    or None if no pattern matches."""
    for pattern, devtype_id in _PATTERNS:
        if pattern.search(raw):
            return devtype_id
    return None
