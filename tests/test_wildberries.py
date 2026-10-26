from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from sniflot.config import Settings
from sniflot.sources.base import Condition, NotFoundError, SearchParams, Sort
from sniflot.sources.wildberries import WildberriesSource, map_product

from .conftest import fixture_json, fixture_text, mock_http

pytestmark = pytest.mark.anyio


def test_map_product_picks_lowest_variant_price_in_kopecks() -> None:
    offer = map_product(fixture_json("wb_search.json")["products"][0])
    assert offer is not None
    assert offer.external_id == "111111"
    assert offer.price == 349900
    assert offer.old_price == 500000
    assert offer.available is True
    assert offer.condition == Condition.NEW
    assert offer.title == "Sonic / Наушники беспроводные X1"
    assert offer.seller == "ООО Звук"
    assert offer.rating == 4.8 and offer.reviews == 1520
    assert offer.url == "https://www.wildberries.ru/catalog/111111/detail.aspx"


def test_map_product_without_price_is_unavailable() -> None:
    offer = map_product(fixture_json("wb_search.json")["products"][1])
    assert offer is not None
    assert offer.price is None
    assert offer.available is False
    assert offer.title == "Наушники проводные с микрофоном"


def test_map_product_legacy_price_fields_and_brand_in_title() -> None:
    offer = map_product(fixture_json("wb_search.json")["products"][2])
    assert offer is not None
    assert (offer.price, offer.old_price) == (49000, 99000)
    assert offer.title == "Sonic чехол для наушников"


def test_map_product_skips_garbage() -> None:
    assert map_product({"id": 1}) is None
    assert map_product({"name": "x"}) is None


def test_parse_ref() -> None:
    wb = WildberriesSource(Settings())
    assert wb.parse_ref("https://www.wildberries.ru/catalog/111111/detail.aspx?size=1") == "111111"
    assert wb.parse_ref("https://wb.ru/catalog/42/detail.aspx") == "42"
    assert wb.parse_ref("https://www.ozon.ru/product/x-1/") is None


async def test_search_sends_filters_and_parses() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=fixture_text("wb_search.json"))

    settings = Settings(wb_search_urls=("https://search.example/v9/search",))
    wb = WildberriesSource(settings, mock_http(handler))
    result = await wb.search(
        SearchParams("наушники", min_price=1000, max_price=5000, sort=Sort.PRICE_ASC)
    )
    assert [o.external_id for o in result.offers] == ["111111", "222222", "333333"]
    assert result.total == 3
    query = parse_qs(urlsplit(str(seen[0].url)).query)
    assert query["query"] == ["наушники"]
    assert query["priceU"] == ["100000;500000"]
    assert query["sort"] == ["priceup"]
    assert query["dest"] == [settings.wb_dest]
    assert query["page"] == ["1"]
    assert "search=%D0%BD" in result.source_url


async def test_search_falls_back_to_next_endpoint_version_and_remembers_it() -> None:
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        if request.url.path == "/v9/search":
            return httpx.Response(404)
        return httpx.Response(200, text=fixture_text("wb_search.json"))

    settings = Settings(
        wb_search_urls=("https://s.example/v9/search", "https://s.example/v18/search")
    )
    wb = WildberriesSource(settings, mock_http(handler))
    await wb.search(SearchParams("a"))
    await wb.search(SearchParams("b"))
    assert hits == ["/v9/search", "/v18/search", "/v18/search"]


async def test_search_paginates_until_short_page() -> None:
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = parse_qs(urlsplit(str(request.url)).query)["page"][0]
        pages.append(page)
        count = 100 if page == "1" else 5
        products = [
            {"id": int(page) * 1000 + i, "name": f"p{i}", "salePriceU": 100} for i in range(count)
        ]
        return httpx.Response(200, json={"products": products})

    wb = WildberriesSource(Settings(), mock_http(handler))
    result = await wb.search(SearchParams("x", pages=3))
    assert pages == ["1", "2"]
    assert len(result.offers) == 105


async def test_used_condition_returns_nothing_without_request() -> None:
    wb = WildberriesSource(Settings(), mock_http(lambda r: pytest.fail("no request expected")))
    result = await wb.search(SearchParams("x", condition=Condition.USED))
    assert result.offers == [] and result.warnings


async def test_query_taken_from_site_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"products": []})

    wb = WildberriesSource(Settings(), mock_http(handler))
    await wb.search(SearchParams("https://www.wildberries.ru/catalog/0/search.aspx?search=ноутбук"))
    assert parse_qs(urlsplit(str(seen[0].url)).query)["query"] == ["ноутбук"]


async def test_fetch_card() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert parse_qs(urlsplit(str(request.url)).query)["nm"] == ["111111"]
        return httpx.Response(200, text=fixture_text("wb_card.json"))

    wb = WildberriesSource(Settings(), mock_http(handler))
    offer = await wb.fetch("https://www.wildberries.ru/catalog/111111/detail.aspx")
    assert offer.price == 329900 and offer.available is True


async def test_fetch_missing_card_is_not_found() -> None:
    wb = WildberriesSource(
        Settings(), mock_http(lambda r: httpx.Response(200, json={"products": []}))
    )
    with pytest.raises(NotFoundError):
        await wb.fetch("123")


async def test_non_json_is_source_error() -> None:
    from sniflot.sources.base import SourceError

    wb = WildberriesSource(Settings(), mock_http(lambda r: httpx.Response(200, text="<html>")))
    with pytest.raises(SourceError):
        await wb.search(SearchParams("x"))


def test_fixture_is_valid_json() -> None:
    json.loads(fixture_text("wb_search.json"))
