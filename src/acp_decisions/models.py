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
class Decision:
    """One ACP appeal decision, with its refusal reasons attached."""
    reference: str
    decision_date: str  # ISO date YYYY-MM-DD
    county_raw: str  # As written on ACP, e.g. "Cork County Council"
    development_type_raw: str  # As written on ACP, free text
    decision_outcome: str  # refused | granted | granted_with_conditions | set_aside | withdrawn | refused_after_review
    scraped_at: str  # ISO 8601 UTC timestamp
    # Optional / nullable
    county: str | None = None  # Mapped CountyId, e.g. 'cork_county'
    site_address: str | None = None
    development_type_id: str | None = None  # Mapped DevelopmentTypeId
    council_decision: str | None = None
    applicant_name_raw: str | None = None
    inspector_report_url: str | None = None
    classified_at: str | None = None
    refusal_reasons: list[RefusalReason] = field(default_factory=list)


@dataclass
class ScrapeError:
    """A non-fatal failure during a scrape, persisted for later review."""
    error_class: str  # transient | parse_error | structural | classification_failed
    occurred_at: str
    reference: str | None = None
    message: str | None = None
