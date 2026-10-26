"""Ozon through a real browser (see :mod:`sniflot.sources.browser`).

Plain HTTP gets a JavaScript challenge, so pages are rendered in Chromium and
then parsed. Search results are read, in order of preference, from:

1. widget state — ``<div id="state-searchResultsV2-…" data-state='{"items": […]}'>``,
   the JSON the page renders its tiles from;
2. the rendered DOM — product links (``/product/<slug>-<id>/``) and the ``₽``
   amounts inside the same tile.

A product page is read from JSON-LD ``Product`` (``offers.price``,
``availability``), the ``webPrice`` widget state and ``<h1>``.

Ozon's markup is not public and changes; every layer is defensive and a page
that has neither products nor a "nothing found" message is reported as an
error (or a block, when it shows a challenge), never as an empty result.
Prices are the regular price, without the Ozon Card discount.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup, Tag

from sniflot.config import Settings
from sniflot.sources.base import (
    BlockedError,
    Condition,
    NotFoundError,
    Offer,
    SearchParams,
    SearchResult,
    Sort,
    SourceError,
    SourceInfo,
)
from sniflot.sources.browser import Page, PageLoader

INFO = SourceInfo(
    id="ozon",
    name="Ozon",
    kind="shop",
    notes=(
        "Не проверено вживую. Нужен браузер (образ full): без JavaScript Ozon отдаёт "
        "антибот-заглушку. Цена без скидки по Ozon Карте; регион — тот, что в профиле "
        "браузера (по умолчанию Москва). При проверке «вы не робот» источник встаёт на паузу."
    ),
    experimental=True,
    block_pause=3600.0,
)

BASE = "https://www.ozon.ru"
_SORTS = {
    Sort.RELEVANCE: "score",
    Sort.PRICE_ASC: "price",
    Sort.PRICE_DESC: "price_desc",
    Sort.NEWEST: "new",
}
_PRODUCT_ID = re.compile(r"/product/(?:[^/?#]*-)?(\d{5,})(?:/|\?|#|$)")
_MONEY = re.compile(r"(\d[\d\s  \xa0]*(?:[.,]\d{1,2})?)\s*₽")
_RATING = re.compile(r"^\s*([1-5][.,]\d)\s*$")
_REVIEWS = re.compile(r"(\d[\d\s  \xa0]*)\s*отзыв", re.I)
_BLOCK_MARKERS = (
    "доступ ограничен",
    "подтвердите, что вы не робот",
    "подтвердите что вы человек",
    "проверяем браузер",
    "пожалуйста, включите javascript",
    "please, enable javascript",
    "antibot",
    "инцидент:",
)
_EMPTY_MARKERS = ("по вашему запросу товаров сейчас нет", "ничего не нашлось", "не нашлось")
_GONE_MARKERS = ("такой страницы не существует", "страница не найдена")
_OUT_OF_STOCK = ("нет в наличии", "товар закончился", "закончился")


def product_id(url: str) -> str | None:
    match = _PRODUCT_ID.search(url)
    return match.group(1) if match else None


def money(text: str | None) -> int | None:
    """``"1 299,50 ₽"`` → 129950 kopecks."""
    if not text:
        return None
    match = _MONEY.search(text)
    if not match:
        return None
    digits = re.sub(r"[\s  \xa0]", "", match.group(1)).replace(",", ".")
    try:
        return round(float(digits) * 100)
    except ValueError:
        return None


def visible_text(soup: BeautifulSoup) -> str:
    for node in soup(["script", "style", "noscript", "template"]):
        node.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return f"{title} {soup.get_text(' ', strip=True)}".lower()


def _walk(data: Any) -> Iterator[Any]:
    yield data
    if isinstance(data, dict):
        for value in data.values():
            yield from _walk(value)
    elif isinstance(data, list):
        for value in data:
            yield from _walk(value)


def _texts(data: Any) -> list[str]:
    out = []
    for node in _walk(data):
        if isinstance(node, dict):
            for key in ("text", "title", "content"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    out.append(value.strip())
    return out


def _clean(text: str) -> str:
    return re.sub(r"<[^>]+>", "", re.sub(r"\s+", " ", text)).strip()


# --- widget state -----------------------------------------------------------


def _has_state(tag: Tag) -> bool:
    return tag.has_attr("data-state")


def widget_states(soup: BeautifulSoup, prefix: str) -> list[dict[str, Any]]:
    states = []
    for node in soup.find_all(_has_state):
        node_id = str(node.get("id") or "")
        if not node_id.startswith(f"state-{prefix}"):
            continue
        try:
            data = json.loads(str(node["data-state"]))
        except ValueError:
            continue
        if isinstance(data, dict):
            states.append(data)
    return states


def _tile_prices(item: dict[str, Any]) -> tuple[int | None, int | None]:
    price = old = None
    for node in _walk(item):
        if not isinstance(node, dict):
            continue
        style = str(node.get("textStyle") or node.get("type") or "").upper()
        text = node.get("text")
        if isinstance(text, str) and "₽" in text:
            if "ORIGINAL" in style or "OLD" in style:
                old = old or money(text)
            elif "PRICE" in style and price is None:
                price = money(text)
        for key, target in (("price", "price"), ("originalPrice", "old")):
            value = node.get(key)
            if isinstance(value, str) and "₽" in value:
                if target == "price" and price is None:
                    price = money(value)
                elif target == "old" and old is None:
                    old = money(value)
    if price is None:
        amounts = [money(t) for t in _texts(item) if "₽" in t]
        known = [a for a in amounts if a]
        if known:
            price = known[0]
            old = old or next((a for a in known[1:] if a > price), None)
    if old is not None and price is not None and old <= price:
        old = None
    return price, old


def _tile_title(item: dict[str, Any]) -> str:
    for node in _walk(item):
        if isinstance(node, dict) and node.get("id") == "name":
            texts = _texts(node)
            if texts:
                return _clean(texts[0])
    candidates = [
        _clean(t)
        for t in _texts(item)
        if "₽" not in t and not _RATING.match(t) and not _REVIEWS.search(t) and len(t) > 12
    ]
    return max(candidates, key=len) if candidates else ""


def _tile_image(item: dict[str, Any]) -> str | None:
    for node in _walk(item):
        if isinstance(node, dict):
            for key in ("link", "image", "src"):
                value = node.get(key)
                if isinstance(value, str) and "ozone.ru" in value and value.startswith("http"):
                    return value
    return None


def map_tile(item: dict[str, Any]) -> Offer | None:
    link = None
    action = item.get("action")
    if isinstance(action, dict) and isinstance(action.get("link"), str):
        link = action["link"]
    if not link:
        link = next(
            (n for n in _walk(item) if isinstance(n, str) and "/product/" in n),
            None,
        )
    pid = product_id(link) if link else None
    if not link or not pid:
        return None
    title = _tile_title(item)
    if not title:
        return None
    price, old = _tile_prices(item)
    texts = [t.lower() for t in _texts(item)]
    rating = next(
        (float(m.group(1).replace(",", ".")) for t in texts if (m := _RATING.match(t))), None
    )
    reviews = next(
        (int(re.sub(r"\D", "", m.group(1))) for t in texts if (m := _REVIEWS.search(t))), None
    )
    unavailable = any(marker in t for t in texts for marker in _OUT_OF_STOCK)
    return Offer(
        source=INFO.id,
        external_id=pid,
        url=urljoin(BASE, link.split("?")[0]),
        title=title,
        price=price,
        old_price=old,
        available=False if unavailable else (True if price else None),
        condition=Condition.NEW,
        image_url=_tile_image(item),
        rating=rating,
        reviews=reviews,
        raw={"sku": pid, "layer": "state"},
    )


# --- DOM fallback -----------------------------------------------------------


def _tile_container(link: Tag, pid: str) -> Tag:
    node: Tag = link
    for _ in range(8):
        parent = node.parent
        if not isinstance(parent, Tag) or parent.name in ("body", "html"):
            break
        ids = {product_id(str(a.get("href") or "")) for a in parent.find_all("a", href=True)}
        ids.discard(None)
        if ids - {pid}:
            break  # the parent already spans a neighbouring tile
        node = parent
        if "₽" in node.get_text(" ", strip=True):
            break
    return node


def dom_tiles(soup: BeautifulSoup) -> list[Offer]:
    offers: dict[str, Offer] = {}
    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        pid = product_id(href)
        if not pid or pid in offers:
            continue
        card = _tile_container(link, pid)
        texts = [_clean(t) for t in card.stripped_strings]
        titles = [
            t
            for t in (_clean(a.get_text(" ", strip=True)) for a in card.find_all("a", href=True))
            if t and "₽" not in t and len(t) > 12
        ]
        title = max(titles, key=len) if titles else ""
        amounts = [m for m in (money(t) for t in texts if "₽" in t) if m]
        if not title or not amounts:
            continue
        price = amounts[0]
        old = next((a for a in amounts[1:] if a > price), None)
        lowered = " ".join(texts).lower()
        offers[pid] = Offer(
            source=INFO.id,
            external_id=pid,
            url=urljoin(BASE, href.split("?")[0]),
            title=title,
            price=price,
            old_price=old,
            available=not any(marker in lowered for marker in _OUT_OF_STOCK),
            condition=Condition.NEW,
            raw={"sku": pid, "layer": "dom"},
        )
    return list(offers.values())


def parse_search(html: str) -> tuple[list[Offer], bool]:
    """Offers on a search page and whether the page says "nothing found"."""
    soup = BeautifulSoup(html, "html.parser")
    offers: dict[str, Offer] = {}
    for state in widget_states(soup, "searchResults") + widget_states(soup, "tileGrid"):
        for item in state.get("items") or []:
            offer = map_tile(item) if isinstance(item, dict) else None
            if offer and offer.external_id not in offers:
                offers[offer.external_id] = offer
    if not offers:
        offers = {o.external_id: o for o in dom_tiles(soup)}
    if offers:
        return list(offers.values()), False
    text = visible_text(soup)
    if any(marker in text for marker in _BLOCK_MARKERS):
        raise BlockedError("Ozon: страница проверки «вы не робот» / доступ ограничен")
    return [], any(marker in text for marker in _EMPTY_MARKERS)


def _json_ld_products(soup: BeautifulSoup) -> list[dict[str, Any]]:
    out = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        for node in _walk(data):
            if isinstance(node, dict) and node.get("@type") in ("Product", ["Product"]):
                out.append(node)
    return out


def parse_product(html: str, url: str) -> Offer:
    soup = BeautifulSoup(html, "html.parser")
    pid = product_id(url)
    title = ""
    price = old = None
    available: bool | None = None
    image = brand = None
    rating = None
    reviews = None

    for product in _json_ld_products(soup):
        title = title or _clean(str(product.get("name") or ""))
        offers = product.get("offers")
        offer = offers[0] if isinstance(offers, list) and offers else offers
        if isinstance(offer, dict):
            value = offer.get("price") or offer.get("lowPrice")
            if value and price is None:
                price = money(f"{value} ₽")
            availability = str(offer.get("availability") or "")
            if availability:
                available = "instock" in availability.lower()
        image_value = product.get("image")
        image = image or (image_value[0] if isinstance(image_value, list) else image_value)
        brand_value = product.get("brand")
        brand = brand or (brand_value.get("name") if isinstance(brand_value, dict) else brand_value)
        aggregate = product.get("aggregateRating")
        if isinstance(aggregate, dict):
            try:
                rating = float(str(aggregate.get("ratingValue")))
                reviews = int(
                    str(aggregate.get("reviewCount") or aggregate.get("ratingCount") or 0)
                )
            except ValueError:
                pass
        pid = pid or (str(product.get("sku")) if product.get("sku") else None)

    price_states = widget_states(soup, "webPrice")
    out_of_stock = bool(widget_states(soup, "webOutOfStock"))
    for state in price_states:
        price = money(str(state.get("price") or "")) or price
        old = money(str(state.get("originalPrice") or "")) or old
        if isinstance(state.get("isAvailable"), bool):
            available = state["isAvailable"]
    if out_of_stock:
        available = False
    structured = bool(title or price_states or out_of_stock)

    h1 = soup.find("h1")
    meta = soup.find("meta", property="og:title")
    heading = _clean(h1.get_text(" ", strip=True)) if h1 else ""
    if not heading and isinstance(meta, Tag):
        heading = _clean(str(meta.get("content") or ""))
    text = visible_text(soup)
    if not structured:
        # No product data at all: this is an error, a removed item or a challenge
        # page (whose <h1> must never become a product title).
        if any(marker in text for marker in _GONE_MARKERS):
            raise NotFoundError("Ozon: товар не найден")
        if any(marker in text for marker in _BLOCK_MARKERS):
            raise BlockedError("Ozon: страница проверки «вы не робот» / доступ ограничен")
        price = money(text)
        if not heading or price is None:
            raise SourceError("Ozon: не удалось разобрать карточку — формат мог измениться")
    title = title or heading
    if not title:
        raise SourceError("Ozon: у карточки нет названия — формат мог измениться")
    if available is None and any(marker in text for marker in _OUT_OF_STOCK):
        available = False
    if old is not None and price is not None and old <= price:
        old = None
    return Offer(
        source=INFO.id,
        external_id=pid or url,
        url=url.split("?")[0],
        title=title,
        price=price,
        old_price=old,
        available=available,
        condition=Condition.NEW,
        image_url=str(image) if image else None,
        brand=str(brand) if brand else None,
        rating=rating,
        reviews=reviews,
        raw={"sku": pid} if pid else {},
    )


class OzonSource:
    info = INFO

    def __init__(
        self,
        settings: Settings,
        search_loader: PageLoader,
        item_loader: PageLoader,
        closer: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self._search = search_loader
        self._item = item_loader
        self._closer = closer

    async def aclose(self) -> None:
        if self._closer is not None:
            await self._closer()

    def parse_ref(self, url: str) -> str | None:
        return product_id(url) if "ozon.ru" in url else None

    def search_url(self, params: SearchParams, page: int = 1) -> str:
        if params.query.startswith("http") and "ozon.ru" in params.query:
            base = params.query
            return base if page == 1 else f"{base}{'&' if '?' in base else '?'}page={page}"
        query: dict[str, str] = {"text": params.query.strip(), "from_global": "true"}
        if params.sort != Sort.RELEVANCE:
            query["sorting"] = _SORTS[params.sort]
        if params.min_price is not None or params.max_price is not None:
            low = params.min_price or 0
            high = params.max_price or 100_000_000
            query["currency_price"] = f"{low:.3f};{high:.3f}"
        if page > 1:
            query["page"] = str(page)
        return f"{BASE}/search/?{urlencode(query)}"

    async def search(self, params: SearchParams) -> SearchResult:
        first_url = self.search_url(params)
        warnings: list[str] = []
        if params.condition == Condition.USED:
            return SearchResult([], first_url, 0, ["Ozon: поиск б/у не поддерживается"])
        if params.region:
            warnings.append("Ozon: регион берётся из профиля браузера, параметр проигнорирован")
        offers: dict[str, Offer] = {}
        for page_no in range(1, max(1, params.pages) + 1):
            page = await self._search(self.search_url(params, page_no))
            _check_status(page)
            found, empty = parse_search(page.html)
            if not found:
                if page_no == 1 and not empty:
                    raise SourceError("Ozon: на странице нет товаров — формат мог измениться")
                break
            before = len(offers)
            for offer in found:
                offers.setdefault(offer.external_id, offer)
            if len(offers) == before:
                break
        return SearchResult(list(offers.values()), first_url, None, warnings)

    async def fetch(self, ref: str) -> Offer:
        pid = ref if ref.isdigit() else product_id(ref)
        if not pid:
            raise SourceError(f"не ссылка на товар Ozon: {ref}")
        url = ref if ref.startswith("http") else f"{BASE}/product/{pid}/"
        page = await self._item(url)
        if page.status in (404, 410):
            raise NotFoundError(f"Ozon: HTTP {page.status}")
        _check_status(page)
        offer = parse_product(page.html, (page.url or url).split("?")[0])
        offer.external_id = pid  # stable id even if the slug in the URL changed
        return offer


def _check_status(page: Page) -> None:
    if page.status == 429:
        raise BlockedError("Ozon: HTTP 429")
