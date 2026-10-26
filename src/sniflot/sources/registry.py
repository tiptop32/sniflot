"""Builds the enabled sources from settings."""

from __future__ import annotations

import logging

from sniflot.config import Settings
from sniflot.sources.avito import AvitoSource
from sniflot.sources.base import Source
from sniflot.sources.browser import BrowserFetcher, playwright_available
from sniflot.sources.ozon import OzonSource
from sniflot.sources.wildberries import WildberriesSource

log = logging.getLogger(__name__)

NO_BROWSER = (
    "нужен браузер: образ full или pip install 'sniflot[browser]' + playwright install chromium"
)


def build_sources(settings: Settings) -> tuple[dict[str, Source], dict[str, str]]:
    """Enabled sources and, separately, enabled-but-unavailable ones with the reason."""
    sources: dict[str, Source] = {}
    unavailable: dict[str, str] = {}
    want_browser = settings.ozon_enabled or (
        settings.avito_enabled and settings.avito_backend == "browser"
    )
    browser = BrowserFetcher(settings) if want_browser and playwright_available() else None

    if settings.avito_enabled:
        if settings.avito_backend == "browser" and browser is None:
            unavailable["avito"] = NO_BROWSER
        else:
            loader = (
                browser.loader("avito", settings.avito_min_interval, "script[data-mfe-state]")
                if browser and settings.avito_backend == "browser"
                else None
            )
            sources["avito"] = AvitoSource(
                settings, browser=loader, closer=browser.aclose if loader and browser else None
            )
    if settings.ozon_enabled:
        if browser is None:
            unavailable["ozon"] = NO_BROWSER
        else:
            sources["ozon"] = OzonSource(
                settings,
                search_loader=browser.loader(
                    "ozon", settings.ozon_min_interval, "a[href*='/product/']"
                ),
                item_loader=browser.loader("ozon", settings.ozon_min_interval, "h1"),
                closer=browser.aclose,
            )
    if settings.wb_enabled:
        sources["wildberries"] = WildberriesSource(settings)
    for sid, reason in unavailable.items():
        log.warning("source %s is enabled but unavailable: %s", sid, reason)
    return sources, unavailable


def source_for_url(sources: dict[str, Source], url: str) -> tuple[Source, str] | None:
    for source in sources.values():
        ref = source.parse_ref(url)
        if ref:
            return source, ref
    return None
