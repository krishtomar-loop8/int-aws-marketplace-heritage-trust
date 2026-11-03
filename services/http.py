"""Read-only HTTP client with rate limiting, retries, and pagination.

Connectors use this for GET-only access to source systems. The client retries on
429 / 5xx (honoring Retry-After) and paces requests with a token-bucket limiter.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlsplit

import requests

from services.logging import get_logger

logger = get_logger(__name__)

# How much of an error response body to log/attach. Enough to capture a source
# system's error message (e.g. a rate-limit/quota note) without dumping payloads.
_ERROR_BODY_MAX = 600


class HttpError(Exception):
    """Raised when a request fails after exhausting retries, or on a 4xx.

    Carries the HTTP ``status`` and a truncated response ``body`` when available,
    so callers/logs can see *why* a source system rejected the request (e.g. an
    Estateably quota/rate message) rather than just the status code.
    """

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _safe_target(url: str) -> str:
    """host + path (query string dropped — it can carry ids/filters)."""
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}"


def _error_body(response: requests.Response) -> str:
    try:
        return response.text[:_ERROR_BODY_MAX]
    except Exception:  # noqa: BLE001 - never let diagnostics mask the real error
        return "<unreadable body>"


class RateLimiter:
    """Token-bucket limiter. Clock and sleep are injectable for testing."""

    def __init__(
        self,
        rate_per_sec: float,
        capacity: float | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity if capacity is not None else rate_per_sec)
        self._tokens = self.capacity
        self._monotonic = monotonic
        self._sleep = sleep
        self._last = monotonic()

    def acquire(self, tokens: float = 1.0) -> None:
        while True:
            now = self._monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return
            self._sleep((tokens - self._tokens) / self.rate)


class HttpClient:
    """GET-only HTTP client with retry + optional rate limiting."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        rate_limiter: RateLimiter | None = None,
        max_retries: int = 3,
        backoff: float = 0.5,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session or requests.Session()
        self.rate_limiter = rate_limiter
        self.max_retries = max_retries
        self.backoff = backoff
        self.timeout = timeout
        self._sleep = sleep

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        attempt = 0
        while True:
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()
            response = self.session.get(url, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self.max_retries:
                    body = _error_body(response)
                    logger.warning(
                        "http request failed after retries",
                        extra={
                            "extra_fields": {
                                "target": _safe_target(url),
                                "status": response.status_code,
                                "body": body,
                            }
                        },
                    )
                    raise HttpError(
                        f"GET {url} failed after retries: {response.status_code}",
                        status=response.status_code,
                        body=body,
                    )
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else self.backoff * (2**attempt)
                self._sleep(delay)
                attempt += 1
                continue
            if response.status_code >= 400:
                # Log the source system's error body so intermittent 4xx (e.g. an
                # Estateably 402/404) can be diagnosed from CloudWatch.
                body = _error_body(response)
                logger.warning(
                    "http request failed",
                    extra={
                        "extra_fields": {
                            "target": _safe_target(url),
                            "status": response.status_code,
                            "body": body,
                        }
                    },
                )
                raise HttpError(
                    f"GET {url} failed: {response.status_code}",
                    status=response.status_code,
                    body=body,
                )
            return response

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.get(url, **kwargs).json()

    def paginate(
        self,
        url: str,
        *,
        items: Callable[[Any], list[Any]],
        next_url: Callable[[Any], str | None],
        **kwargs: Any,
    ) -> Iterator[Any]:
        """Yield items across pages. `params` (if any) applies to the first page only."""
        next_page: str | None = url
        while next_page:
            data = self.get_json(next_page, **kwargs)
            yield from items(data)
            kwargs.pop("params", None)
            next_page = next_url(data)
