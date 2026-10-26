"""Wildberries via its public (undocumented) JSON endpoints.

No login, no browser. Field names and units follow what the site's own
frontend receives: prices are kopecks in ``sizes[].price.product`` (current)
and ``sizes[].price.basic`` (before discount); older responses use
``salePriceU``/``priceU``. Prices do not include the WB Wallet discount.

Known limits (see docs/sources.md): the endpoint version changes without
notice, ``dest`` (region/warehouse) changes price and stock, the site rate
limits at roughly 50 requests/minute with HTTP 429.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlsplit

from sniflot.config import Settings
from sniflot.sources.base import (
    Condition,
    NotFoundError,
    Offer,
    SearchParams,
    SearchResult,
    Sort,
    SourceError,
    SourceInfo,
)
from sniflot.sources.http import HttpClient

INFO = SourceInfo(
    id="wildberries",
    name="Wildberries",
    kind="shop",
    notes=(
        "Публичный JSON-поиск WB. Цена без WB Кошелька; цена и наличие зависят "
        "от региона (dest). Версия эндпоинта меняется — адаптер перебирает известные."
    ),
    block_pause=600.0,
)

_SORTS = {
    Sort.RELEVANCE: "popular",
    Sort.PRICE_ASC: "priceup",
    Sort.PRICE_DESC: "pricedown",
    Sort.NEWEST: "newly",
}
_PRODUCT_URL = re.compile(r"(?:wildberries\.ru|wb\.ru)/catalog/(\d+)(?:/|$)", re.I)


def product_url(nm_id: str) -> str:
    return f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"


class WildberriesSource:
    info = INFO

    def __init__(self, settings: Settings, http: HttpClient | None = None) -> None:
        self._settings = settings
        self._http = http or HttpClient(
            headers={
                # A short UA is what marketplace-mcp found to be rate limited
                # least; the endpoint does not need anything else.
                "User-Agent": settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Referer": "https://www.wildberries.ru/",
            },
            timeout=settings.http_timeout,
            min_interval=settings.wb_min_interval,
        )
        self._search_urls = list(settings.wb_search_urls)

    async def aclose(self) -> None:
        await self._http.aclose()

    def parse_ref(self, url: str) -> str | None:
        match = _PRODUCT_URL.search(url)
        return match.group(1) if match else None

    async def search(self, params: SearchParams) -> SearchResult:
        query = _query_from_url(params.query) or params.query.strip()
        site_url = "https://www.wildberries.ru/catalog/0/search.aspx?search=" + quote_plus(query)
        if params.condition == Condition.USED:
            return SearchResult([], site_url, 0, ["Wildberries продаёт только новые товары"])

        base: dict[str, str | int] = {
            "ab_testing": "false",
            "appType": 1,
            "curr": "rub",
            "dest": params.region or self._settings.wb_dest,
            "lang": "ru",
            "query": query,
            "resultset": "catalog",
            "sort": _SORTS[params.sort],
            "spp": 30,
            "suppressSpellcheck": "false",
        }
        if params.min_price is not None or params.max_price is not None:
            low = (params.min_price or 0) * 100
            high = (params.max_price or 100_000_000) * 100
            base["priceU"] = f"{low};{high}"

        offers: list[Offer] = []
        total: int | None = None
        warnings: list[str] = []
        for page in range(1, max(1, params.pages) + 1):
            payload = await self._search_page({**base, "page": page})
            products = _products(payload)
            if products is None:
                warnings.append("ответ без списка товаров")
                break
            if total is None:
                total = _total(payload)
            offers.extend(o for o in (map_product(p) for p in products) if o is not None)
            if len(products) < 100:
                break
        return SearchResult(offers, site_url, total, warnings)

    async def _search_page(self, params: dict[str, str | int]) -> dict[str, Any]:
        last: Exception | None = None
        for url in list(self._search_urls):
            try:
                response = await self._http.get(url, params, cache_ttl=120)
            except NotFoundError as exc:
                # This endpoint version is gone; try the next and remember the order.
                last = exc
                self._search_urls.remove(url)
                self._search_urls.append(url)
                continue
            return _json(response.text)
        raise SourceError(f"ни одна версия поискового API WB не ответила: {last}")

    async def fetch(self, ref: str) -> Offer:
        nm_id = ref if ref.isdigit() else self.parse_ref(ref)
        if not nm_id:
            raise SourceError(f"не ссылка на товар Wildberries: {ref}")
        response = await self._http.get(
            self._settings.wb_card_url,
            {
                "appType": 1,
                "curr": "rub",
                "dest": self._settings.wb_dest,
                "nm": nm_id,
                "spp": 30,
            },
        )
        products = _products(_json(response.text)) or []
        for product in products:
            if str(product.get("id")) == nm_id:
                offer = map_product(product)
                if offer is not None:
                    return offer
        raise NotFoundError(f"товар {nm_id} не найден")


def _query_from_url(value: str) -> str | None:
    if "wildberries.ru" not in value:
        return None
    found = parse_qs(urlsplit(value).query).get("search")
    return found[0] if found else None


def _json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise SourceError("WB вернул не JSON") from exc
    if not isinstance(data, dict):
        raise SourceError("WB вернул неожиданный JSON")
    return data


def _products(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw = payload.get("products")
    if raw is None and isinstance(payload.get("data"), dict):
        raw = payload["data"].get("products")
    if raw is None:
        return [] if not payload else None
    if not isinstance(raw, list):
        return None
    return [p for p in raw if isinstance(p, dict)]


def _total(payload: dict[str, Any]) -> int | None:
    value = payload.get("total")
    if value is None and isinstance(payload.get("data"), dict):
        value = payload["data"].get("total")
    return _int(value)


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _prices(product: dict[str, Any]) -> tuple[int | None, int | None]:
    """Lowest current variant price and its pre-discount price, kopecks."""
    variants: list[tuple[int, int | None]] = []
    for size in product.get("sizes") or []:
        price = size.get("price") if isinstance(size, dict) else None
        if not isinstance(price, dict):
            continue
        current = _int(price.get("product") or price.get("total") or price.get("sale"))
        basic = _int(price.get("basic"))
        if current and current > 0:
            variants.append((current, basic))
    if variants:
        current, basic = min(variants, key=lambda v: v[0])
        return current, basic if basic and basic > current else None
    current = _int(product.get("salePriceU"))
    basic = _int(product.get("priceU"))
    if current:
        return current, basic if basic and basic > current else None
    return None, None


def _available(product: dict[str, Any], price: int | None) -> bool | None:
    quantity = _int(product.get("totalQuantity"))
    if quantity is not None:
        return quantity > 0
    stocks = [
        stock
        for size in product.get("sizes") or []
        if isinstance(size, dict)
        for stock in size.get("stocks") or []
    ]
    if stocks:
        return True
    return False if price is None else None


def map_product(product: dict[str, Any]) -> Offer | None:
    nm_id = str(product.get("id") or "").strip()
    title = re.sub(r"\s+", " ", str(product.get("name") or "")).strip()
    if not nm_id or not title:
        return None
    price, old_price = _prices(product)
    brand = str(product.get("brand") or "").strip() or None
    return Offer(
        source=INFO.id,
        external_id=nm_id,
        url=product_url(nm_id),
        title=f"{brand} / {title}" if brand and brand.lower() not in title.lower() else title,
        price=price,
        old_price=old_price,
        available=_available(product, price),
        condition=Condition.NEW,
        seller=str(product.get("supplier") or "").strip() or None,
        brand=brand,
        rating=_float(product.get("reviewRating") or product.get("rating")),
        reviews=_int(product.get("feedbacks") or product.get("nmFeedbacks")),
        raw={
            k: product.get(k)
            for k in ("id", "brand", "supplierId", "subjectId", "totalQuantity")
            if product.get(k) is not None
        },
    )
