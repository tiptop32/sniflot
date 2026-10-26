from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from sniflot.config import Settings
from sniflot.sources.base import (
    BlockedError,
    Condition,
    NotFoundError,
    SearchParams,
    Sort,
    SourceError,
)
from sniflot.sources.browser import Page
from sniflot.sources.ozon import OzonSource, money, parse_product, parse_search, product_id

from .conftest import fixture_text

pytestmark = pytest.mark.anyio


def loader(pages: dict[str, str] | str, status: int = 200, seen: list[str] | None = None):  # type: ignore[no-untyped-def]
    async def load(url: str) -> Page:
        if seen is not None:
            seen.append(url)
        if isinstance(pages, str):
            return Page(url, status, fixture_text(pages))
        page = parse_qs(urlsplit(url).query).get("page", ["1"])[0]
        return Page(url, status, fixture_text(pages[page]))

    return load


def test_money_and_product_id() -> None:
    assert money("2 490 ₽") == 249000
    assert money("1 299,50 ₽") == 129950
    assert money("без цены") is None
    assert product_id("https://www.ozon.ru/product/naushniki-sonic-x1-1234567/?at=1") == "1234567"
    assert product_id("/product/1234567/") == "1234567"
    assert product_id("/category/naushniki-15546/") is None


def test_parse_search_widget_state() -> None:
    offers, empty = parse_search(fixture_text("ozon_search_state.html"))
    assert not empty
    by_id = {o.external_id: o for o in offers}
    assert set(by_id) == {"1234567", "7654321", "1111111"}  # category link skipped
    x1 = by_id["1234567"]
    assert x1.title == "Наушники беспроводные Sonic X1, черный"
    assert (x1.price, x1.old_price) == (249000, 499000)
    assert x1.url == "https://www.ozon.ru/product/naushniki-sonic-x1-1234567/"
    assert x1.image_url == "https://cdn1.ozone.ru/s3/multimedia-1/c200/1.jpg"
    assert x1.rating == 4.8 and x1.reviews == 1234 and x1.available is True
    case = by_id["7654321"]
    assert case.price == 39000 and case.old_price is None  # "original" below price is ignored
    assert by_id["1111111"].available is False and by_id["1111111"].price is None


def test_parse_search_dom_fallback() -> None:
    offers, _ = parse_search(fixture_text("ozon_search_dom.html"))
    assert [(o.external_id, o.price, o.old_price) for o in offers] == [
        ("2222222", 129000, 210000),
        ("3333333", 389000, None),
    ]
    assert offers[0].title == "Наушники TWS Mini беспроводные белые"
    assert offers[0].raw["layer"] == "dom"


def test_parse_search_empty_and_challenge() -> None:
    assert parse_search(fixture_text("ozon_search_empty.html")) == ([], True)
    with pytest.raises(BlockedError):
        parse_search(fixture_text("ozon_challenge.html"))
    # A word like "captcha" inside a script on a normal page is not a block.
    assert parse_search(fixture_text("ozon_search_state.html"))[0]


def test_parse_product() -> None:
    url = "https://www.ozon.ru/product/naushniki-sonic-x1-1234567/"
    offer = parse_product(fixture_text("ozon_product.html"), url)
    assert offer.external_id == "1234567"
    assert offer.price == 229000 and offer.old_price == 499000  # without Ozon Card
    assert offer.available is True and offer.brand == "Sonic"
    assert offer.rating == 4.8 and offer.reviews == 1234

    oos = parse_product(
        fixture_text("ozon_product_oos.html"), "https://www.ozon.ru/product/x-1111111/"
    )
    assert (
        oos.available is False and oos.price is None and oos.title == "Наушники Old Model проводные"
    )

    with pytest.raises(NotFoundError):
        parse_product(fixture_text("ozon_gone.html"), url)
    with pytest.raises(BlockedError):
        parse_product(fixture_text("ozon_challenge.html"), url)


def test_search_url() -> None:
    ozon = OzonSource(Settings(), loader("ozon_search_state.html"), loader("ozon_product.html"))
    url = ozon.search_url(SearchParams("наушники", 1000, 5000, sort=Sort.PRICE_ASC), page=2)
    query = parse_qs(urlsplit(url).query)
    assert query["text"] == ["наушники"] and query["sorting"] == ["price"]
    assert query["currency_price"] == ["1000.000;5000.000"] and query["page"] == ["2"]
    assert "sorting" not in parse_qs(urlsplit(ozon.search_url(SearchParams("x"))).query)


async def test_search_paginates_and_stops_on_repeat() -> None:
    seen: list[str] = []
    pages = {
        "1": "ozon_search_state.html",
        "2": "ozon_search_dom.html",
        "3": "ozon_search_dom.html",
    }
    ozon = OzonSource(Settings(), loader(pages, seen=seen), loader("ozon_product.html"))
    result = await ozon.search(SearchParams("наушники", pages=3))
    assert len(result.offers) == 5 and len(seen) == 3


async def test_search_errors() -> None:
    blocked = OzonSource(
        Settings(), loader("ozon_challenge.html", 403), loader("ozon_product.html")
    )
    with pytest.raises(BlockedError):
        await blocked.search(SearchParams("x"))
    odd = OzonSource(Settings(), loader("avito_gone.html"), loader("ozon_product.html"))
    with pytest.raises(SourceError, match="формат"):
        await odd.search(SearchParams("x"))
    empty = OzonSource(Settings(), loader("ozon_search_empty.html"), loader("ozon_product.html"))
    assert (await empty.search(SearchParams("x"))).offers == []
    used = await empty.search(SearchParams("x", condition=Condition.USED))
    assert used.offers == [] and used.warnings


async def test_fetch() -> None:
    seen: list[str] = []
    ozon = OzonSource(
        Settings(), loader("ozon_search_state.html"), loader("ozon_product.html", seen=seen)
    )
    offer = await ozon.fetch("1234567")
    assert seen == ["https://www.ozon.ru/product/1234567/"]
    assert offer.external_id == "1234567" and offer.price == 229000
    assert ozon.parse_ref("https://www.ozon.ru/product/naushniki-1234567/") == "1234567"
    assert ozon.parse_ref("https://www.wildberries.ru/catalog/1/detail.aspx") is None
    gone = OzonSource(Settings(), loader("ozon_search_state.html"), loader("ozon_gone.html", 404))
    with pytest.raises(NotFoundError):
        await gone.fetch("https://www.ozon.ru/product/x-1234567/")
