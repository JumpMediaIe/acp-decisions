"""LGMA national planning-applications fetcher.

Pulls Local Government Management Agency's national planning dataset directly
from the public ArcGIS FeatureServer that backs myplan.ie's National Planning
Application Map. ~492k rows total nationally — ~50k of which are refusals.

Endpoint:
    https://services.arcgis.com/NzlPQPKn5QF9v2US/arcgis/rest/services/IrishPlanningApplications/FeatureServer/0

ArcGIS REST query API:
- ?where=<sql>&outFields=*&resultRecordCount=2000&resultOffset=N&f=json
- maxRecordCount on the layer is 2000, so we paginate via resultOffset.
- Capabilities: Query, Extract — public, no auth.

Default sync target: refusals + cases that link to an ACP appeal (i.e. anything
useful for the analytics layer). Total ~70-80k rows. Full sync runs in ~3-5
minutes at a polite cadence, fits comfortably in the SQLite DB.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import httpx

from acp_decisions.devtype_map import map_devtype

_FEATURESERVER_URL = (
    "https://services.arcgis.com/NzlPQPKn5QF9v2US/"
    "arcgis/rest/services/IrishPlanningApplications/FeatureServer/0/query"
)
_MAX_PAGE_SIZE = 2000  # ArcGIS server-imposed cap

# The default WHERE clause filters to "interesting" applications: anything
# refused, plus anything that ended up at ACP (appealed cases). The
# AppealRefNumber field uses empty strings rather than NULLs for missing
# appeals, so we explicitly exclude empty strings — otherwise the OR matches
# every row in the dataset (~492k).
DEFAULT_WHERE = (
    "Decision LIKE '%Refuse%' "
    "OR (AppealRefNumber IS NOT NULL AND AppealRefNumber <> '')"
)

_FIELDS = [
    "OBJECTID",
    "PlanningAuthority",
    "ApplicationNumber",
    "DevelopmentDescription",
    "DevelopmentAddress",
    "DevelopmentPostcode",
    "ApplicationStatus",
    "ApplicationType",
    "Decision",
    "LandUseCode",
    "AreaofSite",
    "NumResidentialUnits",
    "OneOffHouse",
    "FloorArea",
    "ReceivedDate",
    "DecisionDate",
    "DecisionDueDate",
    "GrantDate",
    "ExpiryDate",
    "AppealRefNumber",
    "AppealStatus",
    "AppealDecision",
    "AppealDecisionDate",
    "AppealSubmittedDate",
    "LinkAppDetails",
    "OneOffKPI",
    "ITMEasting",
    "ITMNorthing",
]


def fetch_planning_applications(
    conn: sqlite3.Connection,
    *,
    where: str = DEFAULT_WHERE,
    progress_callback=None,  # type: ignore[no-untyped-def]
    timeout_s: float = 60.0,
) -> int:
    """Sync the LGMA dataset into `planning_applications`.

    Idempotent — uses INSERT OR REPLACE keyed on OBJECTID, so re-running picks
    up new rows + updates existing ones. Returns the number of rows synced.
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    total_count = _query_count(where, timeout_s=timeout_s)
    if progress_callback is not None:
        progress_callback("count", total_count)

    n_synced = 0
    offset = 0
    with httpx.Client(timeout=timeout_s) as client:
        while offset < total_count:
            page = _query_page(client, where=where, offset=offset)
            features = page.get("features", [])
            if not features:
                break  # safety: server returned nothing despite count > offset
            _upsert_features(conn, features, fetched_at)
            n_synced += len(features)
            offset += len(features)
            if progress_callback is not None:
                progress_callback("page", offset)
    conn.commit()
    return n_synced


def _query_count(where: str, *, timeout_s: float) -> int:
    """Return the total number of features matching `where`."""
    with httpx.Client(timeout=timeout_s) as client:
        resp = client.get(
            _FEATURESERVER_URL,
            params={"where": where, "returnCountOnly": "true", "f": "json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return int(data.get("count", 0))


def _query_page(
    client: httpx.Client,
    *,
    where: str,
    offset: int,
) -> dict[str, Any]:
    """Fetch one page of features."""
    resp = client.get(
        _FEATURESERVER_URL,
        params={
            "where": where,
            "outFields": ",".join(_FIELDS),
            "resultOffset": str(offset),
            "resultRecordCount": str(_MAX_PAGE_SIZE),
            "orderByFields": "OBJECTID",
            "f": "json",
        },
    )
    resp.raise_for_status()
    return resp.json()


def _upsert_features(
    conn: sqlite3.Connection,
    features: list[dict[str, Any]],
    fetched_at: str,
) -> None:
    """Insert or replace a batch of features in planning_applications."""
    # LGMA reassigns OBJECTIDs between fetches, so passing the upstream value
    # would sometimes collide with an existing PK belonging to a different row.
    # Pass NULL on insert so SQLite auto-assigns; the ON CONFLICT branch below
    # updates existing rows by composite key without touching their stable
    # object_id, keeping FKs from council_refusal_reasons intact.
    rows = []
    for feat in features:
        a = feat.get("attributes", {})
        desc = a.get("DevelopmentDescription") or ""
        dev_type_id = map_devtype(desc) if desc else None
        rows.append((
            None,
            a.get("PlanningAuthority") or "",
            a.get("ApplicationNumber") or "",
            desc or None,
            a.get("DevelopmentAddress"),
            a.get("DevelopmentPostcode"),
            a.get("ApplicationStatus"),
            a.get("ApplicationType"),
            a.get("Decision"),
            a.get("LandUseCode"),
            a.get("AreaofSite"),
            a.get("NumResidentialUnits"),
            (a.get("OneOffHouse") or "").strip() or None,
            a.get("FloorArea"),
            _epoch_ms_to_iso(a.get("ReceivedDate")),
            _epoch_ms_to_iso(a.get("DecisionDate")),
            _epoch_ms_to_iso(a.get("DecisionDueDate")),
            _epoch_ms_to_iso(a.get("GrantDate")),
            _epoch_ms_to_iso(a.get("ExpiryDate")),
            a.get("AppealRefNumber"),
            a.get("AppealStatus"),
            a.get("AppealDecision"),
            _epoch_ms_to_iso(a.get("AppealDecisionDate")),
            _epoch_ms_to_iso(a.get("AppealSubmittedDate")),
            a.get("LinkAppDetails"),
            a.get("OneOffKPI"),
            a.get("ITMEasting"),
            a.get("ITMNorthing"),
            fetched_at,
            dev_type_id,
        ))
    # Upsert against the natural key (planning_authority, application_number).
    # The LGMA API reassigns OBJECTIDs between fetches, so keying on object_id
    # would silently insert a fresh row every week. The application's identity
    # is its authority + reference; we let the existing object_id stay even if
    # the upstream gave us a new one, so the foreign-key references from
    # council_refusal_reasons et al. remain intact.
    conn.executemany(
        """
        INSERT INTO planning_applications (
            object_id, planning_authority, application_number,
            development_description, development_address, development_postcode,
            application_status, application_type, decision, land_use_code,
            area_of_site, num_residential_units, one_off_house, floor_area,
            received_date, decision_date, decision_due_date, grant_date,
            expiry_date, appeal_ref_number, appeal_status, appeal_decision,
            appeal_decision_date, appeal_submitted_date, link_app_details,
            one_off_kpi, itm_easting, itm_northing, fetched_at,
            development_type_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(planning_authority, application_number) DO UPDATE SET
            development_description = excluded.development_description,
            development_address     = excluded.development_address,
            development_postcode    = excluded.development_postcode,
            application_status      = excluded.application_status,
            application_type        = excluded.application_type,
            decision                = excluded.decision,
            land_use_code           = excluded.land_use_code,
            area_of_site            = excluded.area_of_site,
            num_residential_units   = excluded.num_residential_units,
            one_off_house           = excluded.one_off_house,
            floor_area              = excluded.floor_area,
            received_date           = excluded.received_date,
            decision_date           = excluded.decision_date,
            decision_due_date       = excluded.decision_due_date,
            grant_date              = excluded.grant_date,
            expiry_date             = excluded.expiry_date,
            appeal_ref_number       = excluded.appeal_ref_number,
            appeal_status           = excluded.appeal_status,
            appeal_decision         = excluded.appeal_decision,
            appeal_decision_date    = excluded.appeal_decision_date,
            appeal_submitted_date   = excluded.appeal_submitted_date,
            link_app_details        = excluded.link_app_details,
            one_off_kpi             = excluded.one_off_kpi,
            itm_easting             = excluded.itm_easting,
            itm_northing            = excluded.itm_northing,
            fetched_at              = excluded.fetched_at,
            development_type_id     = excluded.development_type_id
        """,
        rows,
    )


def _epoch_ms_to_iso(ms: Any) -> str | None:
    """ArcGIS returns dates as Unix epoch milliseconds. Convert to ISO YYYY-MM-DD."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()
    except (ValueError, OSError):
        return None
