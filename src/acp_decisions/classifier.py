"""Classify ACP refusal reasons into taxonomy categories using an LLM.

Two providers are supported:

  - Ollama (local) — http://localhost:11434/api/generate, default model
    `llama3.2:3b`. Runs entirely on your machine; needs no API key.
  - Gemini (Google AI Studio) — Generative Language API, default model
    `gemini-2.5-flash`. Free at our scale; needs `GEMINI_API_KEY` env var.

Both clients implement a `.generate(prompt) -> str` method that returns the
model's raw JSON-formatted response. The classifier asks the model to pick
1-3 category IDs from our fixed taxonomy. Hallucinated IDs are dropped;
truly unrecognised reasons fall back to ['other'].

The classifier is deliberately separate from the scraper: scraping fetches the
canonical text once, and we can re-classify any time as the taxonomy evolves.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import httpx

from acp_decisions.taxonomy import Category, load_taxonomy
from acp_decisions.upsert import update_reason_entities


DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3.2:3b"
DEFAULT_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_TIMEOUT_S = 120.0


class LLMClient(Protocol):
    """Anything with `.generate(prompt) -> str` works for the classifier."""

    def generate(self, prompt: str) -> str: ...

    def close(self) -> None: ...


class OllamaClient:
    """Minimal client for Ollama's /api/generate endpoint with JSON output."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_OLLAMA_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._model = model
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout_s,
            transport=transport,
        )

    def generate(self, prompt: str) -> str:
        """POST /api/generate with format=json + temperature=0; return raw response text."""
        body = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        resp = self._client.post("/api/generate", json=body)
        resp.raise_for_status()
        return resp.json().get("response", "")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class GeminiClient:
    """Minimal client for Google's Generative Language API with JSON output.

    Uses the v1beta REST endpoint directly via httpx — avoids pulling in the
    full `google-genai` SDK for one method. API key is read from the
    GEMINI_API_KEY env var unless passed explicitly.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_GEMINI_URL,
        model: str = DEFAULT_GEMINI_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY env var not set. "
                "Get a free key at https://aistudio.google.com/app/apikey"
            )
        self._api_key = key
        self._base_url = base_url
        self._model = model
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout_s,
            transport=transport,
        )

    def generate(self, prompt: str) -> str:
        """POST /models/{model}:generateContent with JSON output; return raw text."""
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        }
        resp = self._client.post(
            f"/models/{self._model}:generateContent",
            params={"key": self._api_key},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts") or []
        if not parts:
            return ""
        return parts[0].get("text", "")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GeminiClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def build_classification_prompt(reason_text: str, categories: list[Category]) -> str:
    """Compose the structured prompt sent to the model for one reason."""
    cat_lines = "\n".join(
        f"- {c['id']} — {c['name']}: {c['description']}" for c in categories
    )
    return (
        "You analyse refusal reasons from Irish planning appeal decisions.\n\n"
        "Refusal reason text:\n"
        f'"""\n{reason_text.strip()}\n"""\n\n'
        "Available categories (id — name: description):\n"
        f"{cat_lines}\n\n"
        "Tasks:\n"
        "  1. Pick 1 to 3 category_ids from the list above. Use only those IDs.\n"
        '     Use "other" only if no category fits.\n'
        "  2. Write a one-sentence plain-English summary (`summary`) of why the\n"
        "     proposal failed — readable by a non-planner. Avoid quoting jargon.\n"
        "  3. Extract `dev_plan` — the development plan cited (e.g.\n"
        '     "Dublin City Development Plan 2022-2028"), or null if none.\n'
        "  4. Extract `policy_codes` — list of specific policy/section/objective\n"
        '     codes mentioned (e.g. "CPO 7.5", "Section 14.7.9", "Objective CU025").\n'
        "     Empty list if none.\n"
        "  5. Extract `quantitative_violation` — short description of any\n"
        '     dimension/threshold exceeded (e.g. "580 apartments on Z29-zoned land",\n'
        '     "height of 7 storeys"). Null if none.\n'
        "  6. Extract `statutory_test` — statutory test invoked if any (e.g.\n"
        '     "Habitats Directive Article 6", "EIA screening"). Null if none.\n\n'
        "Respond with a single JSON object in exactly this shape:\n"
        "{\n"
        '  "category_ids": ["id1", "id2"],\n'
        '  "summary": "One sentence in plain English.",\n'
        '  "dev_plan": "..." or null,\n'
        '  "policy_codes": ["code1", "code2"],\n'
        '  "quantitative_violation": "..." or null,\n'
        '  "statutory_test": "..." or null\n'
        "}"
    )


@dataclass
class ReasonAnalysis:
    """Structured output of one classifier call."""
    category_ids: list[str]
    summary: str | None
    dev_plan: str | None
    policy_codes: list[str]
    quantitative_violation: str | None
    statutory_test: str | None


def classify_reason(
    client: LLMClient,
    reason_text: str,
    categories: list[Category],
) -> list[str]:
    """Return the LLM-picked category IDs for one refusal reason.

    Falls back to ['other'] on any malformed response. Kept as a thin wrapper
    around analyse_reason so existing callers / tests keep working.
    """
    return analyse_reason(client, reason_text, categories).category_ids


def analyse_reason(
    client: LLMClient,
    reason_text: str,
    categories: list[Category],
) -> ReasonAnalysis:
    """Run the full classifier: categories + summary + entity extraction.

    On any malformed response, returns a ReasonAnalysis with `category_ids=['other']`
    and all entity fields None — the scraper should never crash on a single bad row.
    """
    prompt = build_classification_prompt(reason_text, categories)
    valid_ids = {c["id"] for c in categories}
    try:
        raw = client.generate(prompt)
        parsed = json.loads(raw)
    except (json.JSONDecodeError, httpx.HTTPError, KeyError):
        return ReasonAnalysis(
            category_ids=["other"],
            summary=None,
            dev_plan=None,
            policy_codes=[],
            quantitative_violation=None,
            statutory_test=None,
        )

    ids = parsed.get("category_ids", []) if isinstance(parsed, dict) else []
    if not isinstance(ids, list):
        ids = []
    # Dedupe (preserving order) — Gemma occasionally returns the same ID twice,
    # which would violate the composite PK on reason_categories(reason_id, category_id).
    seen_ids: set[str] = set()
    filtered: list[str] = []
    for i in ids:
        if isinstance(i, str) and i in valid_ids and i not in seen_ids:
            seen_ids.add(i)
            filtered.append(i)
    if not filtered:
        filtered = ["other"]

    pcs = parsed.get("policy_codes", []) if isinstance(parsed, dict) else []
    if not isinstance(pcs, list):
        pcs = []
    seen_pcs: set[str] = set()
    pcs_clean: list[str] = []
    for p in pcs:
        if isinstance(p, str) and p.strip() and p.strip() not in seen_pcs:
            seen_pcs.add(p.strip())
            pcs_clean.append(p.strip())
    pcs = pcs_clean

    return ReasonAnalysis(
        category_ids=filtered,
        summary=_str_or_none(parsed.get("summary")) if isinstance(parsed, dict) else None,
        dev_plan=_str_or_none(parsed.get("dev_plan")) if isinstance(parsed, dict) else None,
        policy_codes=pcs,
        quantitative_violation=_str_or_none(parsed.get("quantitative_violation")) if isinstance(parsed, dict) else None,
        statutory_test=_str_or_none(parsed.get("statutory_test")) if isinstance(parsed, dict) else None,
    )


def _str_or_none(v: object) -> str | None:
    """JSON values come in as str/None/other; coerce to a clean str-or-None."""
    if isinstance(v, str):
        s = v.strip()
        return s if s and s.lower() not in ("null", "none", "n/a") else None
    return None


def classify_unclassified(client: LLMClient, conn: sqlite3.Connection) -> int:
    """Run the analyser over every refusal reason that doesn't yet have a category.

    For each reason: assigns categories AND extracts the entity fields
    (summary, dev_plan, policy_codes, quantitative_violation, statutory_test).
    Updates the parent decision's `classified_at` once done.
    """
    categories = load_taxonomy()
    rows = conn.execute(
        """
        SELECT r.id, r.case_id_url, r.raw_text
        FROM refusal_reasons r
        LEFT JOIN reason_categories rc ON rc.reason_id = r.id
        WHERE rc.reason_id IS NULL
        ORDER BY r.id
        """
    ).fetchall()
    n = 0
    touched_cases: set[int] = set()
    for r in rows:
        analysis = analyse_reason(client, r["raw_text"], categories)
        conn.executemany(
            "INSERT INTO reason_categories (reason_id, category_id) VALUES (?, ?)",
            [(r["id"], cid) for cid in analysis.category_ids],
        )
        update_reason_entities(
            conn,
            int(r["id"]),
            summary=analysis.summary,
            dev_plan=analysis.dev_plan,
            policy_codes=analysis.policy_codes,
            quantitative_violation=analysis.quantitative_violation,
            statutory_test=analysis.statutory_test,
        )
        touched_cases.add(int(r["case_id_url"]))
        n += 1
    if touched_cases:
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "UPDATE decisions SET classified_at = ? WHERE case_id_url = ?",
            [(now, cid) for cid in touched_cases],
        )
    conn.commit()
    return n
