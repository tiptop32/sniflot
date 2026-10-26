"""HTTP layer shared by adapters: rate limit, retries, short cache, block detection."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import httpx

from sniflot.sources.base import BlockedError, NotFoundError, SourceError

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


@dataclass
class _CacheEntry:
    expires: float
    response: httpx.Response


class HttpClient:
    """One instance per source.

    * ``min_interval`` — minimum pause between requests to the source, so a
      search over several pages never hammers the site;
    * network errors and 5xx are retried with exponential backoff;
    * 403/429 raise :class:`BlockedError` immediately (no retry loop);
    * 404/410 raise :class:`NotFoundError`;
    * successful GETs may be cached for ``cache_ttl`` seconds.
    """

    def __init__(
        self,
        *,
        headers: Mapping[str, str],
        timeout: float = 20.0,
        min_interval: float = 1.0,
        retries: int = 2,
        backoff: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers=dict(headers),
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._min_interval = min_interval
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_request = -1e9
        self._cache: dict[str, _CacheEntry] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        url: str,
        params: Mapping[str, str | int] | None = None,
        *,
        cache_ttl: float = 0.0,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        # httpx replaces the URL's own query when ``params`` is passed, even empty.
        key = str(httpx.URL(url, params=dict(params)) if params else httpx.URL(url))
        now = self._clock()
        cached = self._cache.get(key)
        if cached and cached.expires > now:
            return cached.response

        attempt = 0
        while True:
            try:
                response = await self._request(key, headers)
            except httpx.TransportError as exc:
                if attempt >= self._retries:
                    raise SourceError(f"network error: {exc.__class__.__name__}: {exc}") from exc
                attempt += 1
                await self._sleep(self._backoff * 2 ** (attempt - 1))
                continue

            status = response.status_code
            if status in (403, 429):
                retry_after = _retry_after(response.headers.get("Retry-After"))
                raise BlockedError(f"HTTP {status} from {response.url.host}", retry_after)
            if status in (404, 410):
                raise NotFoundError(f"HTTP {status}: {response.url}")
            if status >= 500:
                if attempt >= self._retries:
                    raise SourceError(f"HTTP {status} from {response.url.host}")
                attempt += 1
                await self._sleep(self._backoff * 2 ** (attempt - 1))
                continue
            if status >= 400:
                raise SourceError(f"HTTP {status}: {response.url}")

            if cache_ttl > 0:
                self._purge(now)
                self._cache[key] = _CacheEntry(self._clock() + cache_ttl, response)
            return response

    async def _request(self, url: str, headers: Mapping[str, str] | None) -> httpx.Response:
        async with self._lock:
            wait = self._last_request + self._min_interval - self._clock()
            if wait > 0:
                await self._sleep(wait)
            try:
                response = await self._client.get(url, headers=dict(headers or {}))
                await response.aread()
                return response
            finally:
                self._last_request = self._clock()

    def _purge(self, now: float) -> None:
        if len(self._cache) > 256:
            self._cache = {k: v for k, v in self._cache.items() if v.expires > now}


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
