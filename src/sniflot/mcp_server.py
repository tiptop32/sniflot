"""MCP server over stdio: a thin layer over :class:`Service` for AI agents.

The agent turns natural language into these structured calls; the app itself
does not use an LLM. Shares the SQLite file with the web server (WAL mode).
Requires the optional ``mcp`` dependency: ``pip install 'sniflot[mcp]'``.
"""

from __future__ import annotations

from typing import Any

from sniflot import serialize
from sniflot.config import Settings
from sniflot.services import DISPLAY_SORTS, Service
from sniflot.sources.base import Condition, SearchParams, Sort, SourceError


def build(service: Service) -> Any:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("sniflot")

    @mcp.tool()
    async def search_offers(
        query: str,
        sources: list[str] | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        exclude_words: list[str] | None = None,
        condition: str = "any",
        sort: str = "price_asc",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search marketplaces now. Prices are roubles. sort: relevance, price_asc,
        price_desc, newest, deal (cheapest relative to the other results).
        Results carry deal_score (share below the median) and a suspicious flag."""
        display = sort if sort in DISPLAY_SORTS else "relevance"
        params = SearchParams(
            query,
            min_price,
            max_price,
            None,
            Condition(condition) if condition in ("any", "new", "used") else Condition.ANY,
            Sort(display) if display in Sort.__members__.values() else Sort.RELEVANCE,
        )
        outcome = await service.search(params, sources, exclude_words or [], display)
        return {
            "results": [serialize.row(r) for r in outcome.rows[: max(1, min(limit, 100))]],
            "errors": outcome.errors,
            "warnings": outcome.warnings,
        }

    @mcp.tool()
    def list_sources() -> list[dict[str, Any]]:
        """Enabled sources, their limitations and whether they are paused after a block."""
        return [serialize.source(s, st) for s, st in service.source_states()]

    @mcp.tool()
    def list_watches() -> list[dict[str, Any]]:
        """Listings whose price is being tracked, with current price and last check."""
        return [serialize.watch(w, item) for w, item in service.repo.watches()]

    @mcp.tool()
    async def add_watch(
        url: str, target_price: int | None = None, drop_pct: float = 1.0
    ) -> dict[str, Any]:
        """Start tracking a product or listing by URL. target_price in roubles."""
        try:
            watch = await service.add_watch_by_url(
                url, target_price * 100 if target_price is not None else None, drop_pct
            )
        except (ValueError, SourceError) as exc:
            return {"error": str(exc)}
        return serialize.watch(watch, service.repo.get_listing(watch.listing_id))

    @mcp.tool()
    def price_history(listing_id: int) -> dict[str, Any]:
        """Recorded price changes of a listing (only changes are stored)."""
        item = service.repo.get_listing(listing_id)
        if item is None:
            return {"error": "listing not found"}
        return {
            "listing": serialize.listing(item),
            "history": [serialize.point(p) for p in service.repo.history(listing_id)],
        }

    @mcp.tool()
    def list_searches() -> list[dict[str, Any]]:
        """Saved searches that are re-run on a schedule."""
        return [serialize.search(s) for s in service.repo.searches()]

    @mcp.tool()
    def create_search(
        query: str,
        min_price: int | None = None,
        max_price: int | None = None,
        exclude_words: list[str] | None = None,
        sources: list[str] | None = None,
        interval_minutes: int = 30,
    ) -> dict[str, Any]:
        """Save a search; new listings and price drops will be notified."""
        fields: dict[str, Any] = {
            "query": query,
            "min_price": min_price,
            "max_price": max_price,
            "exclude_words": exclude_words or [],
            "interval_minutes": max(5, interval_minutes),
        }
        if sources:
            fields["sources"] = [s for s in sources if s in service.sources]
        return serialize.search(service.create_search(**fields))

    @mcp.tool()
    def search_results(
        search_id: int, sort: str = "newest", limit: int = 30
    ) -> list[dict[str, Any]]:
        """Listings collected by a saved search."""
        found = service.repo.get_search(search_id)
        if found is None:
            return []
        rows = service.search_results(found, sort if sort in DISPLAY_SORTS else "newest")
        return [serialize.row(r) for r in rows[: max(1, min(limit, 200))]]

    return mcp


def run(settings: Settings) -> None:
    from sniflot.app import build_service

    service = build_service(settings)
    build(service).run()
