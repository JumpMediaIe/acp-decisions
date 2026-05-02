"""Tests for the polite HTTP client."""
from __future__ import annotations

import time

import httpx
import pytest

from acp_decisions.http_client import PoliteClient, RateLimitedError, ScraperError


def test_polite_client_returns_response_text() -> None:
    """Successful GET returns the body."""
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html>ok</html>")
    )
    client = PoliteClient(min_interval_s=0, transport=transport)
    body = client.get("https://example.com/foo")
    assert body == "<html>ok</html>"


def test_polite_client_retries_on_5xx() -> None:
    """A 503 followed by a 200 → returns the 200 body after one retry."""
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    client = PoliteClient(
        min_interval_s=0,
        transport=transport,
        retry_backoffs=(0.0, 0.0, 0.0),
    )
    body = client.get("https://example.com/foo")
    assert body == "ok"
    assert calls["n"] == 2


def test_polite_client_raises_after_exhausted_retries() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(500))
    client = PoliteClient(
        min_interval_s=0,
        transport=transport,
        retry_backoffs=(0.0, 0.0, 0.0),
    )
    with pytest.raises(ScraperError):
        client.get("https://example.com/foo")


def test_polite_client_raises_rate_limit_on_429() -> None:
    """429 is special — surfaces as RateLimitedError after retries exhausted."""
    transport = httpx.MockTransport(lambda req: httpx.Response(429))
    client = PoliteClient(
        min_interval_s=0,
        transport=transport,
        retry_backoffs=(0.0, 0.0, 0.0),
    )
    with pytest.raises(RateLimitedError):
        client.get("https://example.com/foo")


def test_polite_client_enforces_min_interval() -> None:
    """Two back-to-back GETs are spaced by at least min_interval_s."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="ok"))
    client = PoliteClient(min_interval_s=0.1, transport=transport)
    t0 = time.monotonic()
    client.get("https://example.com/a")
    client.get("https://example.com/b")
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.09  # allow tiny scheduler slack
