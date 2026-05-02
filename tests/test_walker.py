"""Tests for the search-page walker."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from acp_decisions.walker import fetch_case_ids_for_type, parse_listing


@pytest.fixture
def lh_listing(fixtures_dir: Path) -> str:
    return (fixtures_dir / "listing_lh.html").read_text(encoding="utf-8")


def test_parse_listing_returns_unique_ids(lh_listing: str) -> None:
    ids = parse_listing(lh_listing)
    assert len(ids) > 100  # We saw 245 in this fixture
    assert len(ids) == len(set(ids)), "ids must be unique"


def test_parse_listing_returns_ints(lh_listing: str) -> None:
    ids = parse_listing(lh_listing)
    assert all(isinstance(i, int) for i in ids)


def test_parse_listing_handles_empty_page() -> None:
    assert parse_listing("<html><body>No cases here</body></html>") == []


def test_fetch_case_ids_for_type_uses_client() -> None:
    """fetch_case_ids_for_type GETs `/en-ie/cases?type={code}` and parses the result."""
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        body = """
        <html><body>
          <a href="/en-ie/case/100001">case 1</a>
          <a href="/en-ie/case/100002">case 2</a>
          <a href="/en-ie/case/100001">dup</a>
        </body></html>
        """
        return httpx.Response(200, text=body)

    from acp_decisions.http_client import PoliteClient
    transport = httpx.MockTransport(handler)
    client = PoliteClient(min_interval_s=0, transport=transport)
    ids = fetch_case_ids_for_type(client, "PA")
    assert "type=PA" in captured["url"]
    assert sorted(ids) == [100001, 100002]
