"""Persistence layer: upsert Decisions, DocumentLinks, RefusalReasons.

Each function is idempotent — running the same scrape twice produces the same
DB state. Documents and reasons are replaced wholesale on each upsert because
ACP can re-issue an Order or correct an inspector report; we trust the latest
scrape.
"""
from __future__ import annotations

import sqlite3

from acp_decisions.models import Decision, DocumentLink, RefusalReason, ScrapeError


def upsert_decision(conn: sqlite3.Connection, decision: Decision) -> None:
    """Insert a Decision row, or update it on case_id_url conflict."""
    conn.execute(
        """
        INSERT INTO decisions (
            case_id_url, abp_reference, pa_reference,
            decision_date, county, county_raw, site_address,
            development_type_id, development_type_raw,
            case_type_raw, decision_outcome, decision_outcome_raw,
            council_decision, applicant_name_raw, scraped_at, classified_at
        ) VALUES (
            :case_id_url, :abp_reference, :pa_reference,
            :decision_date, :county, :county_raw, :site_address,
            :development_type_id, :development_type_raw,
            :case_type_raw, :decision_outcome, :decision_outcome_raw,
            :council_decision, :applicant_name_raw, :scraped_at, :classified_at
        )
        ON CONFLICT(case_id_url) DO UPDATE SET
            abp_reference        = excluded.abp_reference,
            pa_reference         = excluded.pa_reference,
            decision_date        = excluded.decision_date,
            county               = excluded.county,
            county_raw           = excluded.county_raw,
            site_address         = excluded.site_address,
            development_type_id  = excluded.development_type_id,
            development_type_raw = excluded.development_type_raw,
            case_type_raw        = excluded.case_type_raw,
            decision_outcome     = excluded.decision_outcome,
            decision_outcome_raw = excluded.decision_outcome_raw,
            council_decision     = excluded.council_decision,
            applicant_name_raw   = excluded.applicant_name_raw,
            scraped_at           = excluded.scraped_at,
            classified_at        = excluded.classified_at
        """,
        {
            "case_id_url": decision.case_id_url,
            "abp_reference": decision.abp_reference,
            "pa_reference": decision.pa_reference,
            "decision_date": decision.decision_date,
            "county": decision.county,
            "county_raw": decision.county_raw,
            "site_address": decision.site_address,
            "development_type_id": decision.development_type_id,
            "development_type_raw": decision.development_type_raw,
            "case_type_raw": decision.case_type_raw,
            "decision_outcome": decision.decision_outcome,
            "decision_outcome_raw": decision.decision_outcome_raw,
            "council_decision": decision.council_decision,
            "applicant_name_raw": decision.applicant_name_raw,
            "scraped_at": decision.scraped_at,
            "classified_at": decision.classified_at,
        },
    )
    conn.commit()


def upsert_documents(
    conn: sqlite3.Connection,
    case_id_url: int,
    documents: list[DocumentLink],
) -> None:
    """Replace the document set for a case (delete-then-insert)."""
    conn.execute("DELETE FROM documents WHERE case_id_url = ?", (case_id_url,))
    conn.executemany(
        "INSERT INTO documents (case_id_url, doc_type, url, fetched_at) "
        "VALUES (?, ?, ?, ?)",
        [(case_id_url, d.doc_type, d.url, d.fetched_at) for d in documents],
    )
    conn.commit()


def upsert_reasons(
    conn: sqlite3.Connection,
    case_id_url: int,
    reasons: list[RefusalReason],
) -> list[int]:
    """Replace the reasons for a case; return the inserted IDs in order.

    Replacement keeps the schema clean if a re-scrape produces a different
    parse — e.g. a regex tweak alters reason segmentation.
    """
    conn.execute("DELETE FROM refusal_reasons WHERE case_id_url = ?", (case_id_url,))
    inserted_ids: list[int] = []
    for r in reasons:
        cur = conn.execute(
            "INSERT INTO refusal_reasons (case_id_url, reason_number, raw_text) "
            "VALUES (?, ?, ?)",
            (case_id_url, r.reason_number, r.raw_text),
        )
        inserted_ids.append(int(cur.lastrowid))  # type: ignore[arg-type]
    conn.commit()
    return inserted_ids


def update_reason_entities(
    conn: sqlite3.Connection,
    reason_id: int,
    *,
    summary: str | None,
    dev_plan: str | None,
    policy_codes: list[str],
    quantitative_violation: str | None,
    statutory_test: str | None,
) -> None:
    """Persist classifier-extracted entities onto an existing refusal_reasons row."""
    import json as _json
    conn.execute(
        """
        UPDATE refusal_reasons
           SET summary                = ?,
               dev_plan               = ?,
               policy_codes           = ?,
               quantitative_violation = ?,
               statutory_test         = ?
         WHERE id = ?
        """,
        (
            summary,
            dev_plan,
            _json.dumps(policy_codes) if policy_codes else None,
            quantitative_violation,
            statutory_test,
            reason_id,
        ),
    )
    conn.commit()


def record_scrape_error(conn: sqlite3.Connection, error: ScrapeError) -> None:
    conn.execute(
        "INSERT INTO scrape_errors (case_id_url, error_class, message, occurred_at, resolved_at) "
        "VALUES (?, ?, ?, ?, NULL)",
        (error.case_id_url, error.error_class, error.message, error.occurred_at),
    )
    conn.commit()
