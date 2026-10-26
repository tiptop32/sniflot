"""Avito (priority source; not yet verified against the live site).

Avito puts the page state into ``<script type="mime/invalid"
data-mfe-state="true">`` as HTML-escaped JSON; search results live at
``loaderData.data.catalog.items`` and the next page at ``catalog.pager.next``.
An older layout used ``window.__initialData__`` (URI-encoded JSON); both are
handled by looking for the catalog block by shape.

Avito protects itself (IP firewall, CAPTCHA, TLS fingerprinting). This adapter
is deliberately polite: pages come from a plain HTTP client or a real Chromium
(``SNIFLOT_AVITO_BACKEND=browser``), with long pauses between requests, no
cookie farming, no proxy rotation, no CAPTCHA solving. A block page raises
:class:`BlockedError` and the service pauses the source. On many IPs, including
datacenter ones, Avito will simply not work — see docs/sources.md.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlencode, urljoin

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
from sniflot.sources.browser import PageLoader
from sniflot.sources.http import HttpClient

INFO = SourceInfo(
    id="avito",
    name="Avito",
    kind="classifieds",
    notes=(
        "Не проверено вживую. Avito блокирует автоматический доступ (фаервол по IP, капча); "
        "адаптер не обходит защиту и при блокировке ставит источник на паузу. Загрузка "
        "страниц — HTTP или настоящий браузер (SNIFLOT_AVITO_BACKEND=browser). "
        "Для точных фильтров вставьте ссылку на поиск Avito вместо текста."
    ),
    experimental=True,
    block_pause=3600.0,
)

BASE = "https://www.avito.ru"
_SORTS = {Sort.RELEVANCE: None, Sort.NEWEST: "104", Sort.PRICE_ASC: "1", Sort.PRICE_DESC: "2"}
_LISTING_URL = re.compile(r"avito\.ru/[^?#]*_(\d{6,})(?:[?#]|$)", re.I)
_FIREWALL_MARKERS = ("firewall-container", "js-firewall-form", "firewallCaptcha")
_BLOCK_TEXT = ("доступ ограничен", "проблема с ip", "подтвердите, что вы не робот")
_GONE_TEXT = ("снято с публикации", "объявление удалено", "объявление не найдено")
_MS_THRESHOLD = 100_000_000_000


class _StateScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.states: list[str] = []
        self.title = ""
        self._in_state = False
        self._in_title = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "script" and a.get("type") == "mime/invalid" and a.get("data-mfe-state"):
            self._in_state, self._buf = True, []
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_state:
            self.states.append("".join(self._buf))
            self._in_state = False
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_state:
            self._buf.append(data)
        elif self._in_title:
            self.title += data


def page_state(html: str) -> list[dict[str, Any]]:
    """All decoded state objects found on the page."""
    parser = _StateScripts()
    parser.feed(html)
    states: list[dict[str, Any]] = []
    for raw in parser.states:
        try:
            data = json.loads(html_lib.unescape(raw))
        except ValueError:
            continue
        if isinstance(data, dict):
            states.append(data)
    match = re.search(r'window\.__initialData__\s*=\s*"(.*?)";', html)
    if match:
        try:
            data = json.loads(unquote(match.group(1)))
        except ValueError:
            data = None
        if isinstance(data, dict):
            states.append(data)
    return states


def check_blocked(html: str) -> None:
    lowered = html[:200_000].lower()
    if any(marker.lower() in lowered for marker in _FIREWALL_MARKERS):
        raise BlockedError("Avito: страница фаервола (блокировка по IP / капча)")
    parser = _StateScripts()
    parser.feed(html[:20_000])
    if any(text in parser.title.lower() for text in _BLOCK_TEXT):
        raise BlockedError(f"Avito: {parser.title.strip()}")


def _find(data: Any, predicate: Any, depth: int = 0) -> dict[str, Any] | None:
    if depth > 12:
        return None
    if isinstance(data, dict):
        if predicate(data):
            return data
        children: Any = data.values()
    elif isinstance(data, list):
        children = data
    else:
        return None
    for child in children:
        found = _find(child, predicate, depth + 1)
        if found is not None:
            return found
    return None


def _is_catalog(d: dict[str, Any]) -> bool:
    items = d.get("items")
    return isinstance(items, list) and any(
        isinstance(i, dict) and i.get("id") and (i.get("urlPath") or i.get("title")) for i in items
    )


def find_catalog(states: list[dict[str, Any]]) -> dict[str, Any] | None:
    for state in states:
        found = _find(state, _is_catalog)
        if found is not None:
            return found
    return None


def find_redirect(states: list[dict[str, Any]]) -> str | None:
    for state in states:
        found = _find(state, lambda d: bool(d.get("redirected") and isinstance(d.get("url"), str)))
        if found is not None:
            return str(found["url"])
    return None


def _image(item: dict[str, Any]) -> str | None:
    for image in item.get("images") or item.get("gallery") or []:
        if not isinstance(image, dict):
            continue
        sized = [(k, v) for k, v in image.items() if re.fullmatch(r"\d+x\d+", k) and v]
        if sized:
            return str(max(sized, key=lambda kv: int(kv[0].split("x")[0]))[1])
        for key in ("url", "src"):
            if image.get(key):
                return str(image[key])
    return None


def _location(item: dict[str, Any]) -> str | None:
    raw_geo = item.get("geo")
    geo: dict[str, Any] = raw_geo if isinstance(raw_geo, dict) else {}
    parts = [str(geo["formattedAddress"])] if geo.get("formattedAddress") else []
    for ref in geo.get("geoReferences") or []:
        if isinstance(ref, dict) and ref.get("content"):
            parts.append(str(ref["content"]))
    if parts:
        return ", ".join(parts)
    for key, sub in (("addressDetailed", "locationName"), ("location", "name")):
        value = item.get(key)
        if isinstance(value, dict) and value.get(sub):
            return str(value[sub])
    return None


def _published(item: dict[str, Any]) -> datetime | None:
    raw = item.get("sortTimeStamp")
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    seconds = raw / 1000 if raw > _MS_THRESHOLD else raw
    return datetime.fromtimestamp(seconds, tz=UTC)


def _price(item: dict[str, Any]) -> int | None:
    detailed = item.get("priceDetailed")
    value = detailed.get("value") if isinstance(detailed, dict) else item.get("price")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value * 100)
    if isinstance(value, str):
        digits = re.sub(r"\D", "", value)
        return int(digits) * 100 if digits else None
    return None


def map_item(item: dict[str, Any]) -> Offer | None:
    item_id = str(item.get("id") or "").strip()
    if not item_id or item_id == "0":
        return None
    path = item.get("urlPath") or item.get("url")
    url = urljoin(BASE, str(path)) if path else f"{BASE}/{item_id}"
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    return Offer(
        source=INFO.id,
        external_id=item_id,
        url=url.split("?")[0],
        title=title,
        price=_price(item),
        available=True,
        condition=Condition.ANY,
        location=_location(item),
        image_url=_image(item),
        published_at=_published(item),
        raw={
            k: item.get(k)
            for k in ("id", "sellerId", "isPromotion", "categoryId")
            if item.get(k) is not None
        },
    )


class AvitoSource:
    info = INFO

    def __init__(
        self,
        settings: Settings,
        http: HttpClient | None = None,
        browser: PageLoader | None = None,
        closer: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Pages come from ``browser`` when given (a real Chromium), else plain HTTP."""
        self._settings = settings
        self._browser = browser
        self._closer = closer
        self._http = http or HttpClient(
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
            timeout=settings.http_timeout,
            min_interval=settings.avito_min_interval,
            retries=1,
        )

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._closer is not None:
            await self._closer()

    async def _html(self, url: str, cache_ttl: float = 0.0) -> str:
        if self._browser is None:
            response = await self._http.get(url, cache_ttl=cache_ttl)
            html = response.text
        else:
            page = await self._browser(url)
            if page.status in (404, 410):
                raise NotFoundError(f"HTTP {page.status}: {url}")
            check_blocked(page.html)
            if page.status in (403, 429):
                raise BlockedError(f"Avito: HTTP {page.status}")
            html = page.html
        check_blocked(html)
        return html

    def parse_ref(self, url: str) -> str | None:
        match = _LISTING_URL.search(url)
        return match.group(1) if match else None

    def search_url(self, params: SearchParams) -> str:
        if params.query.startswith(("http://", "https://")) and "avito.ru" in params.query:
            return params.query
        query: dict[str, str] = {"q": params.query.strip()}
        if params.min_price is not None:
            query["pmin"] = str(params.min_price)
        if params.max_price is not None:
            query["pmax"] = str(params.max_price)
        sort = _SORTS[params.sort]
        if sort:
            query["s"] = sort
        region = params.region or self._settings.avito_region
        return f"{BASE}/{region}?{urlencode(query)}"

    async def search(self, params: SearchParams) -> SearchResult:
        url = self.search_url(params)
        warnings: list[str] = []
        if params.condition != Condition.ANY:
            warnings.append(
                "фильтр «новое/б/у» у Avito зависит от категории — задайте его в ссылке"
            )
        offers: list[Offer] = []
        next_url: str | None = url
        redirects = 0
        page = 0
        while next_url and page < max(1, params.pages):
            html = await self._html(next_url, cache_ttl=120)
            states = page_state(html)
            catalog = find_catalog(states)
            if catalog is None:
                redirect = find_redirect(states)
                if redirect and redirects < 2:
                    redirects += 1
                    next_url = urljoin(BASE, redirect)
                    continue
                if states and page == 0 and '"items":[]' in html.replace(" ", ""):
                    break  # genuinely empty result
                raise SourceError("Avito: на странице нет каталога — формат мог измениться")
            page += 1
            offers.extend(o for o in (map_item(i) for i in catalog["items"]) if o)
            pager = catalog.get("pager")
            nxt = pager.get("next") if isinstance(pager, dict) else None
            next_url = urljoin(BASE, str(nxt)) if nxt else None
        total = None
        return SearchResult(offers, url, total, warnings)

    async def fetch(self, ref: str) -> Offer:
        if not ref.startswith("http"):
            raise SourceError("для Avito нужна ссылка на объявление")
        html = await self._html(ref)
        lowered = html.lower()
        if any(text in lowered for text in _GONE_TEXT):
            raise NotFoundError("объявление снято с публикации")
        states = page_state(html)
        item = None
        for state in states:
            item = _find(
                state,
                lambda d: bool(d.get("id")) and bool(d.get("title")) and "priceDetailed" in d,
            )
            if item:
                break
        offer = map_item(item) if item else None
        if offer is None:
            raise SourceError("Avito: не удалось разобрать объявление — формат мог измениться")
        offer.url = ref.split("?")[0]
        return offer
