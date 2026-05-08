"""Fetch refusal reasons from councils that use the agileapplications.ie portal.

The Agile portal (used by Cork County, South Dublin, Fingal, Wexford in Ireland —
~26k applications, ~23k of which are refusals) is an Angular SPA that gets its
data from a public unauthenticated JSON API.

API discovery (recorded so future-you doesn't have to redo it):

  identity API:    https://identity.agileapplications.ie
  per-tenant API:  https://planningapi.agileapplications.ie  (returned by
                     GET {identity}/api/configuration/API_URL with appropriate headers)

  Required headers on every per-tenant request:
    x-client:  TENANT CODE (e.g. CORKCOCO) — fetched from the identity API
                using the URL slug as input
    x-product: CITIZENPORTAL
    x-service: PA

  Endpoints used:
    GET {api}/api/application/search?reference=<urlencoded ref>
        → { total, results: [ { id, reference, decisionText, ... } ] }
    GET {api}/api/application/{id}/conditions
        → { decisionText, applicationPrescriptions: [
              { shortPrescription, longPrescription, prescriptionCode, orderNumber }
            ] }

  prescriptionCode values: 'R' = Reason (refusal), 'C' = Condition (granted-with).
  We filter to 'R' for refusal-reason extraction.

The slug-to-tenant-code mapping is fetched once at startup and cached in-process.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx


_IDENTITY_BASE = "https://identity.agileapplications.ie"
_LINK_SLUG_RE = re.compile(r"agileapplications\.ie/([a-z0-9-]+)/", re.I)


@dataclass
class AgileReason:
    """One refusal reason from a council application."""
    reason_number: int
    short_prescription: str | None
    long_prescription: str


class AgileApiError(Exception):
    """Non-recoverable error fetching from the Agile API."""


class AgileApiClient:
    """Cached client for the agileapplications.ie API.

    Uses one underlying httpx.Client. Lookups for tenant code and tenant API URL
    are cached in-process so we only do one identity-API roundtrip per slug per
    process lifetime.
    """

    def __init__(
        self,
        *,
        timeout_s: float = 30.0,
        user_agent: str = "planningcheck.ie scraper - public records reproduction (contact: contact@planningcheck.ie)",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            timeout=timeout_s,
            headers={"User-Agent": user_agent},
            transport=transport,
        )
        self._tenant_code: dict[str, str] = {}  # slug -> code (e.g. corkcoco -> CORKCOCO)
        self._tenant_api: dict[str, str] = {}   # slug -> base URL for that tenant's planning API

    def slug_from_link(self, link: str | None) -> str | None:
        """Extract the council slug from a LinkAppDetails URL."""
        if not link:
            return None
        m = _LINK_SLUG_RE.search(link)
        return m.group(1).lower() if m else None

    def _tenant_headers(self, slug: str) -> dict[str, str]:
        """Headers required for per-tenant API calls."""
        return {
            "x-client": self._get_tenant_code(slug),
            "x-product": "CITIZENPORTAL",
            "x-service": "PA",
        }

    def _get_tenant_code(self, slug: str) -> str:
        """Resolve a slug (e.g. 'corkcoco') to a tenant code (e.g. 'CORKCOCO')."""
        if slug not in self._tenant_code:
            resp = self._client.get(
                f"{_IDENTITY_BASE}/api/client/get",
                params={"url": slug},
            )
            resp.raise_for_status()
            data = resp.json()
            code = data.get("code")
            if not code:
                raise AgileApiError(f"identity API returned no code for slug {slug!r}")
            self._tenant_code[slug] = code
        return self._tenant_code[slug]

    def _get_tenant_api(self, slug: str) -> str:
        """Resolve a slug to its planning-API base URL (e.g. https://planningapi.agileapplications.ie)."""
        if slug not in self._tenant_api:
            resp = self._client.get(
                f"{_IDENTITY_BASE}/api/configuration/API_URL",
                headers=self._tenant_headers(slug),
            )
            resp.raise_for_status()
            data = resp.json()
            value = data.get("value")
            if not value:
                raise AgileApiError(f"no planning API URL configured for slug {slug!r}")
            self._tenant_api[slug] = value.rstrip("/")
        return self._tenant_api[slug]

    def search_application_id(self, slug: str, reference: str) -> int | None:
        """Look up the internal application ID for a council reference (e.g. '26/0435').

        Returns None if the API can't find it.
        """
        api = self._get_tenant_api(slug)
        resp = self._client.get(
            f"{api}/api/application/search",
            params={"reference": reference},
            headers=self._tenant_headers(slug),
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        results = data.get("results") or []
        if not results:
            return None
        # The reference can occasionally match more than one row across years.
        # Prefer an exact-string match if present.
        for r in results:
            if (r.get("reference") or "").strip().lower() == reference.strip().lower():
                return r.get("id")
        return results[0].get("id")

    def fetch_refusal_reasons(self, slug: str, app_id: int) -> list[AgileReason]:
        """Fetch refusal reasons for an application.

        Returns reasons (prescriptionCode == 'R') in source order. Empty list
        if the application has no recorded reasons.
        """
        api = self._get_tenant_api(slug)
        resp = self._client.get(
            f"{api}/api/application/{app_id}/conditions",
            headers=self._tenant_headers(slug),
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        prescriptions = data.get("applicationPrescriptions") or []
        reasons: list[AgileReason] = []
        for p in prescriptions:
            if (p.get("prescriptionCode") or "").upper() != "R":
                continue
            long_text = (p.get("longPrescription") or "").strip()
            if not long_text:
                continue
            reasons.append(AgileReason(
                reason_number=int(p.get("orderNumber") or 0) or len(reasons) + 1,
                short_prescription=(p.get("shortPrescription") or None),
                long_prescription=long_text,
            ))
        # Stable ordering by reason_number ascending
        reasons.sort(key=lambda r: r.reason_number)
        return reasons

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AgileApiClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
