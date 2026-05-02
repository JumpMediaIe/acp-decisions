"""Domain dataclasses used across the scraper, classifier, and tests."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RefusalReason:
    """One numbered reason from an ACP decision's "Reasons and Considerations" block."""
    reason_number: int
    raw_text: str
    # Optional id once persisted; classifier writes categories keyed on this
    id: int | None = None


@dataclass
class DocumentLink:
    """One PDF document linked from an ACP case page (Order, Inspector Report, etc.)."""
    doc_type: str  # 'order' | 'inspector_report' | 'direction' | 'bmr' | 'other'
    url: str
    fetched_at: str | None = None


@dataclass
class Decision:
    """One ACP appeal decision, with its refusal reasons attached.

    Three reference formats coexist (see docs/discovery-2026-05-02.md):
      - case_id_url:   numeric URL key, e.g. 315183
      - abp_reference: canonical 'ABP-{id}-{yy}', e.g. 'ABP-315183-22'
      - pa_reference:  page-header form, e.g. 'LH02.319750'
    """
    case_id_url: int
    decision_date: str  # ISO date YYYY-MM-DD
    county_raw: str  # As written on ACP page, e.g. "Cork County Council"
    development_type_raw: str  # As written, free text
    decision_outcome: str  # normalised bucket
    decision_outcome_raw: str  # verbatim free text
    scraped_at: str  # ISO 8601 UTC timestamp
    # Optional / nullable
    abp_reference: str | None = None
    pa_reference: str | None = None
    county: str | None = None  # Mapped CountyId, e.g. 'cork_county'
    site_address: str | None = None
    development_type_id: str | None = None  # Mapped DevelopmentTypeId
    case_type_raw: str | None = None
    council_decision: str | None = None
    applicant_name_raw: str | None = None
    classified_at: str | None = None
    refusal_reasons: list[RefusalReason] = field(default_factory=list)
    documents: list[DocumentLink] = field(default_factory=list)


@dataclass
class ScrapeError:
    """A non-fatal failure during a scrape, persisted for later review."""
    error_class: str  # transient | parse_error | structural | classification_failed
    occurred_at: str
    case_id_url: int | None = None
    message: str | None = None
