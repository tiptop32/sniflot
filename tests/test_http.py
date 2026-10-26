from __future__ import annotations

import httpx
import pytest

from sniflot.sources.base import BlockedError, NotFoundError, SourceError
from sniflot.sources.http import HttpClient

from .conftest import mock_http

pytestmark = pytest.mark.anyio


async def test_retries_5xx_then_succeeds() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(502 if len(calls) < 2 else 200, text="ok")

    http = mock_http(handler)
    response = await http.get("https://x.example/a")
    assert response.text == "ok"
    assert len(calls) == 2


async def test_gives_up_after_retries() -> None:
    http = mock_http(lambda r: httpx.Response(503), retries=2)
    with pytest.raises(SourceError, match="503"):
        await http.get("https://x.example/a")


async def test_429_is_a_block_with_retry_after_and_no_retry() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    with pytest.raises(BlockedError) as info:
        await mock_http(handler).get("https://x.example/a")
    assert info.value.retry_after == 120
    assert len(calls) == 1


async def test_403_is_a_block() -> None:
    with pytest.raises(BlockedError):
        await mock_http(lambda r: httpx.Response(403)).get("https://x.example/a")


async def test_404_is_not_found() -> None:
    with pytest.raises(NotFoundError):
        await mock_http(lambda r: httpx.Response(404)).get("https://x.example/a")


async def test_network_error_retried_then_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(SourceError, match="network error"):
        await mock_http(handler, retries=1).get("https://x.example/a")


async def test_cache_serves_repeat_requests() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=str(len(calls)))

    http = mock_http(handler)
    first = await http.get("https://x.example/a", {"q": "1"}, cache_ttl=60)
    second = await http.get("https://x.example/a", {"q": "1"}, cache_ttl=60)
    third = await http.get("https://x.example/a", {"q": "2"}, cache_ttl=60)
    assert first.text == second.text == "1"
    assert third.text == "2"


async def test_min_interval_spaces_requests() -> None:
    now = [100.0]
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    http = HttpClient(
        headers={},
        min_interval=5,
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        sleep=sleep,
        clock=lambda: now[0],
    )
    await http.get("https://x.example/1")
    now[0] += 1
    await http.get("https://x.example/2")
    assert waits == [pytest.approx(4)]


async def test_url_query_is_kept_without_params() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    http = mock_http(handler)
    await http.get("https://x.example/a?q=1&p=2")
    await http.get("https://x.example/b", {"q": "x y"})
    assert seen == ["https://x.example/a?q=1&p=2", "https://x.example/b?q=x+y"]
