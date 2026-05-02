"""Tests for the Ollama-based refusal-reason classifier."""
from __future__ import annotations

import json
import sqlite3

import httpx

from acp_decisions.classifier import (
    OllamaClient,
    build_classification_prompt,
    classify_reason,
    classify_unclassified,
)
from acp_decisions.models import Decision, RefusalReason
from acp_decisions.taxonomy import load_taxonomy, seed_categories
from acp_decisions.upsert import upsert_decision, upsert_reasons


def _ollama_transport(json_body: str) -> httpx.MockTransport:
    """Mock Ollama /api/generate that returns json_body as the model's response."""
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/generate"
        return httpx.Response(200, json={"response": json_body, "done": True})
    return httpx.MockTransport(handler)


def test_build_prompt_includes_reason_and_categories() -> None:
    cats = load_taxonomy()
    prompt = build_classification_prompt("the proposal contravenes the zoning", cats)
    assert "the proposal contravenes the zoning" in prompt
    assert "zoning_contravention" in prompt
    assert "Material contravention of zoning objective" in prompt
    assert '"category_ids"' in prompt


def test_classify_reason_returns_valid_ids() -> None:
    transport = _ollama_transport(
        json.dumps({"category_ids": ["zoning_contravention", "natura_appropriate_assessment"]})
    )
    client = OllamaClient(transport=transport)
    result = classify_reason(client, "some reason text", load_taxonomy())
    assert result == ["zoning_contravention", "natura_appropriate_assessment"]


def test_classify_reason_filters_unknown_ids() -> None:
    """If the LLM hallucinates an ID, drop it."""
    transport = _ollama_transport(
        json.dumps({"category_ids": ["zoning_contravention", "made_up_id"]})
    )
    client = OllamaClient(transport=transport)
    result = classify_reason(client, "some reason", load_taxonomy())
    assert result == ["zoning_contravention"]


def test_classify_reason_handles_empty_response() -> None:
    """A malformed/empty model response falls back to ['other']."""
    transport = _ollama_transport("not valid json at all")
    client = OllamaClient(transport=transport)
    result = classify_reason(client, "some reason", load_taxonomy())
    assert result == ["other"]


def test_classify_reason_at_least_one_id() -> None:
    """Empty category_ids list also falls back to ['other']."""
    transport = _ollama_transport(json.dumps({"category_ids": []}))
    client = OllamaClient(transport=transport)
    result = classify_reason(client, "some reason", load_taxonomy())
    assert result == ["other"]


def test_classify_unclassified_persists_categories(temp_db: sqlite3.Connection) -> None:
    """End-to-end: unclassified reasons get classified, results land in reason_categories."""
    seed_categories(temp_db)
    decision = Decision(
        case_id_url=999,
        decision_date="2024-01-01",
        county_raw="Cork County Council",
        development_type_raw="House extension",
        decision_outcome="refused",
        decision_outcome_raw="Refuse Permission",
        scraped_at="2026-05-02T00:00:00+00:00",
    )
    upsert_decision(temp_db, decision)
    upsert_reasons(
        temp_db,
        999,
        [RefusalReason(reason_number=1, raw_text="Material contravention of zoning")],
    )
    transport = _ollama_transport(
        json.dumps({"category_ids": ["zoning_contravention"]})
    )
    client = OllamaClient(transport=transport)
    n = classify_unclassified(client, temp_db)
    assert n == 1
    rows = temp_db.execute(
        "SELECT category_id FROM reason_categories"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["category_id"] == "zoning_contravention"


def test_classify_unclassified_skips_already_classified(temp_db: sqlite3.Connection) -> None:
    """Reasons that already have categories should be skipped."""
    seed_categories(temp_db)
    decision = Decision(
        case_id_url=999,
        decision_date="2024-01-01",
        county_raw="Cork County Council",
        development_type_raw="House extension",
        decision_outcome="refused",
        decision_outcome_raw="Refuse Permission",
        scraped_at="2026-05-02T00:00:00+00:00",
    )
    upsert_decision(temp_db, decision)
    ids = upsert_reasons(temp_db, 999, [RefusalReason(reason_number=1, raw_text="x")])
    # Pre-populate one category for that reason
    temp_db.execute(
        "INSERT INTO reason_categories (reason_id, category_id) VALUES (?, ?)",
        (ids[0], "other"),
    )
    temp_db.commit()
    transport = _ollama_transport(
        json.dumps({"category_ids": ["zoning_contravention"]})
    )
    client = OllamaClient(transport=transport)
    n = classify_unclassified(client, temp_db)
    assert n == 0  # already classified — skipped
