"""Builds the enabled sources from settings."""

from __future__ import annotations

from sniflot.config import Settings
from sniflot.sources.avito import AvitoSource
from sniflot.sources.base import Source
from sniflot.sources.wildberries import WildberriesSource


def build_sources(settings: Settings) -> dict[str, Source]:
    sources: dict[str, Source] = {}
    if settings.wb_enabled:
        sources["wildberries"] = WildberriesSource(settings)
    if settings.avito_enabled:
        sources["avito"] = AvitoSource(settings)
    return sources


def source_for_url(sources: dict[str, Source], url: str) -> tuple[Source, str] | None:
    for source in sources.values():
        ref = source.parse_ref(url)
        if ref:
            return source, ref
    return None
