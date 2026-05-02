"""Classify ACP refusal reasons into taxonomy categories using a local LLM.

We talk to Ollama (https://ollama.ai) over its HTTP API at
http://localhost:11434/api/generate. The model is asked to pick 1-3 category
IDs from our fixed taxonomy and return them as JSON. Hallucinated IDs are
dropped; truly unrecognised reasons fall back to ['other'].

The classifier is deliberately separate from the scraper: scraping fetches the
canonical text once, and we can re-classify any time as the taxonomy evolves.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import httpx

from acp_decisions.taxonomy import Category, load_taxonomy


DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2:3b"
DEFAULT_TIMEOUT_S = 120.0


class OllamaClient:
    """Minimal client for Ollama's /api/generate endpoint with JSON output."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_MODEL,
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


def build_classification_prompt(reason_text: str, categories: list[Category]) -> str:
    """Compose the structured prompt sent to the model for one reason."""
    cat_lines = "\n".join(
        f"- {c['id']} — {c['name']}: {c['description']}" for c in categories
    )
    return (
        "You classify refusal reasons from Irish planning appeal decisions.\n\n"
        "Refusal reason text:\n"
        f'"""\n{reason_text.strip()}\n"""\n\n'
        "Available categories (id — name: description):\n"
        f"{cat_lines}\n\n"
        "Pick 1 to 3 categories that best describe this reason. Use ONLY the IDs from the list above.\n"
        "Use \"other\" only if no category fits.\n\n"
        'Respond with a single JSON object in exactly this shape: {"category_ids": ["id1", "id2"]}'
    )


def classify_reason(
    client: OllamaClient,
    reason_text: str,
    categories: list[Category],
) -> list[str]:
    """Return the LLM-picked category IDs for one refusal reason.

    Falls back to ['other'] on any malformed response, empty list, or all-invalid
    IDs — never returns an empty list.
    """
    prompt = build_classification_prompt(reason_text, categories)
    valid_ids = {c["id"] for c in categories}
    try:
        raw = client.generate(prompt)
        parsed = json.loads(raw)
        ids = parsed.get("category_ids", [])
    except (json.JSONDecodeError, httpx.HTTPError, KeyError):
        return ["other"]
    if not isinstance(ids, list):
        return ["other"]
    filtered = [i for i in ids if isinstance(i, str) and i in valid_ids]
    return filtered or ["other"]


def classify_unclassified(client: OllamaClient, conn: sqlite3.Connection) -> int:
    """Classify every refusal reason that doesn't yet have a category. Returns count.

    Updates the parent decision's `classified_at` once all of its reasons are done.
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
        cat_ids = classify_reason(client, r["raw_text"], categories)
        conn.executemany(
            "INSERT INTO reason_categories (reason_id, category_id) VALUES (?, ?)",
            [(r["id"], cid) for cid in cat_ids],
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
