from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from sniflot.config import Settings
from sniflot.sources.avito import AvitoSource, find_catalog, map_item, page_state
from sniflot.sources.base import BlockedError, NotFoundError, SearchParams, Sort, SourceError

from .conftest import fixture_text, mock_http

pytestmark = pytest.mark.anyio


def test_page_state_and_catalog() -> None:
    catalog = find_catalog(page_state(fixture_text("avito_catalog.html")))
    assert catalog is not None
    offers = [o for o in map(map_item, catalog["items"]) if o]
    assert len(offers) == 2  # the id=0 banner is skipped
    first, second = offers
    assert first.external_id == "4001234567"
    assert first.price == 3_800_000
    assert first.url == "https://www.avito.ru/moskva/telefony/iphone_13_128_gb_4001234567"
    assert first.location == "Москва, ул. Пушкина, 1, Тверская"
    assert first.image_url == "https://img.example/l.jpg"
    assert first.published_at == datetime.fromtimestamp(1761400000, tz=UTC)
    assert second.title == 'iPhone 13 128 Гб синий & "как новый"'
    assert second.url.endswith("iphone_13_4007654321")
    assert second.location == "Химки"
    assert second.published_at == datetime.fromtimestamp(1761300000, tz=UTC)


def test_search_url() -> None:
    avito = AvitoSource(Settings(avito_region="moskva"))
    url = avito.search_url(
        SearchParams("iphone 13", min_price=10000, max_price=50000, sort=Sort.NEWEST)
    )
    parts = urlsplit(url)
    assert parts.path == "/moskva"
    assert parse_qs(parts.query) == {
        "q": ["iphone 13"],
        "pmin": ["10000"],
        "pmax": ["50000"],
        "s": ["104"],
    }
    own = "https://www.avito.ru/moskva/telefony?q=iphone&s=104"
    assert avito.search_url(SearchParams(own)) == own


async def test_search_follows_pager_and_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("p") == "2":
            return httpx.Response(200, text=fixture_text("avito_catalog_p2.html"))
        if request.url.path == "/moskva/telefony":
            return httpx.Response(200, text=fixture_text("avito_catalog.html"))
        return httpx.Response(200, text=fixture_text("avito_redirect.html"))

    avito = AvitoSource(Settings(), mock_http(handler))
    result = await avito.search(SearchParams("iphone 13", pages=2))
    assert [o.external_id for o in result.offers] == ["4001234567", "4007654321", "4009999999"]


async def test_firewall_page_is_a_block() -> None:
    avito = AvitoSource(
        Settings(),
        mock_http(lambda r: httpx.Response(200, text=fixture_text("avito_firewall.html"))),
    )
    with pytest.raises(BlockedError):
        await avito.search(SearchParams("iphone"))


async def test_unknown_layout_is_an_error_not_an_empty_result() -> None:
    avito = AvitoSource(
        Settings(),
        mock_http(lambda r: httpx.Response(200, text="<html><body>new design</body></html>")),
    )
    with pytest.raises(SourceError):
        await avito.search(SearchParams("iphone"))


async def test_fetch_item_and_gone() -> None:
    avito = AvitoSource(
        Settings(), mock_http(lambda r: httpx.Response(200, text=fixture_text("avito_item.html")))
    )
    url = "https://www.avito.ru/moskva/telefony/iphone_13_128_gb_4001234567"
    offer = await avito.fetch(url)
    assert offer.price == 3_600_000 and offer.url == url
    assert avito.parse_ref(url) == "4001234567"

    gone = AvitoSource(
        Settings(), mock_http(lambda r: httpx.Response(200, text=fixture_text("avito_gone.html")))
    )
    with pytest.raises(NotFoundError):
        await gone.fetch(url)
