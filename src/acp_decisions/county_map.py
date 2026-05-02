"""Maps the free-text "Planning Authority" field on ACP decisions to a stable
CountyId matching planningcheck.ie's lib/counties.ts.

Note: ACP usually writes the planning-authority name verbatim. Limerick and
Waterford were merged into single city-and-county councils in 2014 — we map
both to a single id ('limerick' / 'waterford') because the planningcheck.ie
codebase already does the same merge.
"""
from __future__ import annotations

import re

# Canonical mapping. Keys are normalised (lowercase, ASCII-fold, single spaces).
_COUNTY_MAP: dict[str, str] = {
    "cork county council": "cork_county",
    "cork city council": "cork_city",
    "dublin city council": "dublin_city",
    "south dublin county council": "south_dublin",
    "dun laoghaire-rathdown county council": "dun_laoghaire_rathdown",
    "dun laoghaire rathdown county council": "dun_laoghaire_rathdown",
    "fingal county council": "fingal",
    "galway city council": "galway_city",
    "galway county council": "galway_county",
    "kerry county council": "kerry",
    "clare county council": "clare",
    "limerick city and county council": "limerick",
    "waterford city and county council": "waterford",
    "wexford county council": "wexford",
    "kilkenny county council": "kilkenny",
    "carlow county council": "carlow",
    "wicklow county council": "wicklow",
    "kildare county council": "kildare",
    "meath county council": "meath",
    "louth county council": "louth",
    "monaghan county council": "monaghan",
    "cavan county council": "cavan",
    "donegal county council": "donegal",
    "sligo county council": "sligo",
    "leitrim county council": "leitrim",
    "mayo county council": "mayo",
    "roscommon county council": "roscommon",
    "longford county council": "longford",
    "westmeath county council": "westmeath",
    "offaly county council": "offaly",
    "laois county council": "laois",
    "tipperary county council": "tipperary",
}

_ASCII_FOLD: dict[str, str] = {
    "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
    "Á": "A", "É": "E", "Í": "I", "Ó": "O", "Ú": "U",
}


def _normalise(raw: str) -> str:
    """Lowercase, ASCII-fold accents, collapse whitespace."""
    folded = "".join(_ASCII_FOLD.get(c, c) for c in raw)
    return re.sub(r"\s+", " ", folded.strip().lower())


def map_county(raw: str) -> str | None:
    """Return the CountyId for an ACP planning-authority name, or None."""
    return _COUNTY_MAP.get(_normalise(raw))
