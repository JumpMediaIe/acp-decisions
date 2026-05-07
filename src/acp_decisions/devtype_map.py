"""Maps free-text "development description" strings to a DevelopmentTypeId.

Used by both the ACP scraper (for case "Type of development" fields) and the
LGMA fetcher (for council "DevelopmentDescription" fields). The two data
sources phrase things differently — councils tend to write `dwellinghouse`
(one word) where ACP often says `single dwelling`. Patterns are deliberately
broad to absorb both.

The mapping is heuristic — ordered list of (regex pattern, devtype id) pairs.
First match wins. Returns None when no pattern matches.

Order matters: specific patterns must come BEFORE generic ones (e.g. LRD
before single-dwelling, side-extension before plain "extension").
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # ---- Large multi-unit residential developments (specific numbered counts) ----
    # Must precede single-dwelling so "138 dwellings" doesn't fall through.
    (re.compile(r"\b\d{2,4}\s*(?:no\.?\s*)?(?:apartments?|residential\s+units?|dwelling\s+units?|dwellings?|dwellinghouses?|units)\b", re.I), "large_residential_development"),
    (re.compile(r"\blarge[-\s]scale\s+(?:residential\s+)?development", re.I), "large_residential_development"),
    (re.compile(r"\bstrategic\s+housing\s+development\b", re.I), "large_residential_development"),
    (re.compile(r"\bbuild[-\s]to[-\s]rent\b", re.I), "large_residential_development"),
    (re.compile(r"\bresidential\s+development\b.*\b(?:dwellings|dwellinghouses|units|apartments)\b", re.I), "large_residential_development"),

    # ---- Specialised use types (must precede generic patterns) ----
    (re.compile(r"\bstudent\s+accommodation\b", re.I), "student_accommodation"),
    (re.compile(r"\b(?:hotel|guesthouse|guest\s+house|aparthotel)\b", re.I), "hotel"),
    (re.compile(r"\b(?:restaurant|public\s+house|licensed\s+premises|caf[ée]|takeaway)\b", re.I), "restaurant_pub"),
    (re.compile(r"\b(?:supermarket|retail\s+(?:unit|store|warehouse|park)|shop\s+(?:front|unit))\b", re.I), "retail"),
    (re.compile(r"\boffice(?:\s+(?:block|building|development|space|unit))\b", re.I), "office"),
    (re.compile(r"\b(?:warehouse|industrial\s+(?:unit|building|estate|development)|factory|manufacturing)\b", re.I), "industrial_warehouse"),
    (re.compile(r"\b(?:school|primary\s+school|secondary\s+school|college|education(?:al)?\s+(?:facility|building))\b", re.I), "school_education"),
    (re.compile(r"\b(?:cr[èe]che|childcare|playschool|montessori|preschool|nursery)\b", re.I), "childcare"),
    (re.compile(r"\b(?:church|chapel|place\s+of\s+worship|parish\s+hall|mosque|temple)\b", re.I), "religious"),
    (re.compile(r"\b(?:cemetery|crematorium|columbarium|burial\s+ground)\b", re.I), "cemetery"),
    (re.compile(r"\bdata\s+centre\b", re.I), "data_centre"),
    (re.compile(r"\b(?:gym|leisure\s+centre|fitness\s+centre|sports\s+(?:hall|facility))\b", re.I), "leisure_sports"),
    (re.compile(r"\bnursing\s+home|care\s+home\b", re.I), "nursing_care_home"),
    (re.compile(r"\b(?:caravan|mobile\s+home|campsite|camping\s+pod|holiday\s+park|chalet)\b", re.I), "caravan_camping"),
    (re.compile(r"\b(?:petrol|service)\s+station\b|\bforecourt\b", re.I), "service_station"),
    (re.compile(r"\b(?:equestrian|horse|stables?|riding\s+arena)\b", re.I), "equestrian"),
    (re.compile(r"\b(?:quarry|sand\s+(?:and\s+)?gravel\s+pit|importation\s+of\s+(?:soil|inert))\b", re.I), "quarry"),

    # ---- Communications / signage ----
    (re.compile(r"\b(?:telecommunications?|antenn[ae]|telecoms?|mobile\s+phone\s+(?:mast|tower)|monopole|microwave\s+dish)\b", re.I), "telecoms"),
    (re.compile(r"\b(?:advertising\s+sign|signage|fascia\s+sign|illuminated\s+sign|billboard|advertisement\s+(?:hoarding|panel))\b", re.I), "signage"),

    # ---- Renewable energy ----
    (re.compile(r"\bsolar\s+farm\b", re.I), "solar_farm"),
    (re.compile(r"\bsolar\s+(?:panels|pv|photovoltaic|array)", re.I), "solar_panels_house"),
    (re.compile(r"\bwind\s+(?:farm|turbine\s+(?:array|cluster))", re.I), "wind_farm"),
    (re.compile(r"\bwind\s+turbine\b", re.I), "wind_turbine_single"),
    (re.compile(r"\bheat\s+pump\b", re.I), "heat_pump_air"),
    (re.compile(r"\bbattery\s+(?:storage|energy)\b", re.I), "battery_storage"),
    (re.compile(r"\b(?:biogas|biomass|anaerobic\s+digestion)\b", re.I), "renewable_other"),

    # ---- Extensions (specific to houses) ----
    (re.compile(r"\bside\s+(?:and\s+rear|extension)\b", re.I), "house_extension_side"),
    (re.compile(r"\b(?:rear|two[-\s]storey|single[-\s]storey|first[-\s]floor|second[-\s]floor|ground\s+floor|wrap[-\s]around)\s+extension\b", re.I), "house_extension_rear"),
    (re.compile(r"\bextension\s+to\s+(?:dwelling|dwellinghouse|bungalow|house|the\s+rear)\b", re.I), "house_extension_rear"),
    (re.compile(r"\b(?:rear|side)\s+extensions?\b", re.I), "house_extension_rear"),
    (re.compile(r"\bporch\s+extension\b", re.I), "porch"),
    (re.compile(r"\bextension\b", re.I), "house_extension_rear"),

    # ---- Demolition + replacement (must precede generic dwelling) ----
    (re.compile(r"\bdemolish?\s+(?:existing\s+)?(?:dwelling|dwellinghouse|bungalow|house|cottage|residence)\b.*\b(?:construct|new|replace|erect)\b", re.I | re.DOTALL), "demolition_replacement_dwelling"),
    (re.compile(r"\bdemolition\s+(?:and|of)\s+replacement\s+(?:of\s+)?dwelling", re.I), "demolition_replacement_dwelling"),
    (re.compile(r"\bdemolition\s+of\s+(?:existing\s+)?(?:dwellinghouse|bungalow|cottage|residence)\b.*\bconstruct", re.I | re.DOTALL), "demolition_replacement_dwelling"),
    (re.compile(r"\breplacement\s+(?:dwelling|dwellinghouse|bungalow|house)\b", re.I), "demolition_replacement_dwelling"),

    # ---- Single dwellings (the broad catch — handles 'dwellinghouse' as one word) ----
    (re.compile(r"\b(?:dwellinghouse|dwelling[-\s]house|bungalow|cottage)s?\b", re.I), "new_house_rural"),
    (re.compile(r"\b(?:single|two|one[-\s]and[-\s]half|two\s+and\s+half)[-\s](?:storey|story)\s+(?:type\s+)?(?:dwelling|residence|house)\b", re.I), "new_house_rural"),
    (re.compile(r"\b(?:single|new)\s+dwelling\b(?!\s+extension)", re.I), "new_house_rural"),
    (re.compile(r"\bdwelling\b(?!\s+extension)(?!\s+units?)", re.I), "new_house_rural"),
    (re.compile(r"\bnew\s+house\b", re.I), "new_house_rural"),
    (re.compile(r"\bdetached\s+(?:two[-\s]storey|single[-\s]storey)?\s*house\b", re.I), "new_house_rural"),

    # ---- Outbuildings ----
    (re.compile(r"\b(?:garage|carport|car\s+port)\b", re.I), "garage_shed_outbuilding"),
    (re.compile(r"\b(?:garden\s+room|garden\s+office|shed|workshop|outbuilding|store(?:\s+building)?|domestic\s+store)\b", re.I), "garage_shed_outbuilding"),

    # ---- Site works / boundary ----
    (re.compile(r"\b(?:vehicular\s+entrance|new\s+entrance|access\s+(?:road|onto)|boundary\s+(?:wall|fence)|gates\s+and\s+piers)\b", re.I), "site_access_boundary"),

    # ---- Roof / dormer / attic ----
    (re.compile(r"\battic\s+conversion\b", re.I), "attic_conversion"),
    (re.compile(r"\bdormer\b", re.I), "attic_conversion"),

    # ---- Driveway ----
    (re.compile(r"\b(?:driveway|paving)\b", re.I), "driveway"),

    # ---- Agricultural ----
    (re.compile(r"\bagricultural\s+(?:building|shed|store)\b", re.I), "agricultural_building"),
    (re.compile(r"\b(?:slatted\s+(?:shed|unit)|farm\s+(?:building|shed|yard)|silage\s+pit|cubicle\s+shed)\b", re.I), "agricultural_building"),
    (re.compile(r"\b(?:cattle|livestock|poultry|pig)\s+(?:shed|unit|house)\b", re.I), "agricultural_building"),

    # ---- Change of use / retention ----
    (re.compile(r"\bchange\s+of\s+use\b", re.I), "change_of_use_general"),
    (re.compile(r"\bretention\s+(?:permission\s+)?of\b", re.I), "retention"),
    (re.compile(r"\bretention\b", re.I), "retention"),

    # ---- Multi-unit fallbacks ----
    (re.compile(r"\bresidential\s+development\b", re.I), "multi_unit_housing"),
    (re.compile(r"\bhousing\s+(?:scheme|estate|development)\b", re.I), "multi_unit_housing"),
    (re.compile(r"\b(?:dwellings|dwellinghouses|apartments)\b", re.I), "multi_unit_housing"),
]


def map_devtype(raw: str) -> str | None:
    """Return the DevelopmentTypeId for a description, or None if no pattern matches."""
    if not raw:
        return None
    for pattern, devtype_id in _PATTERNS:
        if pattern.search(raw):
            return devtype_id
    return None
