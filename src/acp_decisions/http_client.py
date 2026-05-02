"""Polite HTTP client: rate-limited, retried, identifying User-Agent.

Ground rules:
- 1 request per `min_interval_s` (default 1.5 s) — enforced as a sleep before each request.
- 3 retries on 5xx and 429, with exponential backoff (5 s / 30 s / 5 min by default).
- 4xx other than 429 is fatal — raises ScraperError immediately.
- User-Agent identifies the scraper and provides a contact email.
"""
from __future__ import annotations

import time
from collections.abc import Iterable

import httpx


DEFAULT_USER_AGENT = (
    "planningcheck.ie ACP archiver - public records reproduction "
    "for transparency. Contact: contact@planningcheck.ie"
)
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_RETRY_BACKOFFS_S: tuple[float, ...] = (5.0, 30.0, 300.0)
DEFAULT_MIN_INTERVAL_S = 1.5


class ScraperError(Exception):
    """Non-recoverable HTTP failure."""


class RateLimitedError(ScraperError):
    """We're being rate-limited. Caller should back off significantly."""


class PoliteClient:
    """Synchronous httpx client with rate limiting and retries."""

    def __init__(
        self,
        *,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        user_agent: str = DEFAULT_USER_AGENT,
        retry_backoffs: Iterable[float] = DEFAULT_RETRY_BACKOFFS_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._min_interval_s = min_interval_s
        self._retry_backoffs = tuple(retry_backoffs)
        self._last_request_t: float | None = None
        self._client = httpx.Client(
            timeout=timeout_s,
            headers={"User-Agent": user_agent},
            transport=transport,
        )

    def _wait(self) -> None:
        if self._last_request_t is None:
            return
        elapsed = time.monotonic() - self._last_request_t
        remaining = self._min_interval_s - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def get(self, url: str) -> str:
        """GET `url`, return body as text. Raises on persistent failure."""
        return self._request(url).text

    def get_bytes(self, url: str) -> bytes:
        """GET `url`, return raw bytes (for PDFs and other binary content)."""
        return self._request(url).content

    def _request(self, url: str) -> httpx.Response:
        """Shared GET with rate limit + retry. Returns the successful response."""
        last_exc: Exception | None = None
        for attempt, backoff in enumerate((0.0,) + self._retry_backoffs):
            if backoff > 0:
                time.sleep(backoff)
            self._wait()
            self._last_request_t = time.monotonic()
            try:
                resp = self._client.get(url)
            except httpx.TransportError as e:
                last_exc = e
                continue

            if 200 <= resp.status_code < 300:
                return resp
            if resp.status_code == 429:
                if attempt == len(self._retry_backoffs):
                    raise RateLimitedError(f"429 on {url} after {attempt + 1} attempts")
                continue
            if 500 <= resp.status_code < 600:
                if attempt == len(self._retry_backoffs):
                    raise ScraperError(f"{resp.status_code} on {url} after {attempt + 1} attempts")
                continue
            # 4xx other than 429 — fatal
            raise ScraperError(f"{resp.status_code} on {url}: {resp.text[:200]}")

        raise ScraperError(f"GET {url} failed: {last_exc}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
