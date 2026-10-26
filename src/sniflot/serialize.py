"""Plain-dict views of domain objects for JSON responses and MCP."""

from __future__ import annotations

from typing import Any

from sniflot.repo import Event, Listing, PricePoint, Search, SourceState, Watch, iso
from sniflot.services import ResultRow
from sniflot.sources.base import Source


def money(kopecks: int | None) -> float | None:
    return None if kopecks is None else kopecks / 100


def listing(item: Listing) -> dict[str, Any]:
    return {
        "id": item.id,
        "source": item.source,
        "external_id": item.external_id,
        "url": item.url,
        "title": item.title,
        "price": money(item.price),
        "old_price": money(item.old_price),
        "currency": item.currency,
        "available": item.available,
        "condition": item.condition,
        "seller": item.seller,
        "location": item.location,
        "image_url": item.image_url,
        "brand": item.brand,
        "rating": item.rating,
        "reviews": item.reviews,
        "published_at": iso(item.published_at),
        "status": item.status,
        "favorite": item.favorite,
        "first_seen_at": iso(item.first_seen_at),
        "last_seen_at": iso(item.last_seen_at),
    }


def row(r: ResultRow) -> dict[str, Any]:
    return {
        **listing(r.listing),
        "deal_score": r.peer.score,
        "suspicious": r.peer.suspicious,
        "history_score": r.history,
    }


def point(p: PricePoint) -> dict[str, Any]:
    return {"at": iso(p.at), "price": money(p.price), "available": p.available}


def search(s: Search) -> dict[str, Any]:
    return {
        "id": s.id,
        "name": s.name,
        "query": s.query,
        "sources": s.sources,
        "min_price": s.min_price,
        "max_price": s.max_price,
        "region": s.region,
        "condition": s.condition,
        "sort": s.sort,
        "exclude_words": s.exclude_words,
        "pages": s.pages,
        "interval_minutes": s.interval_minutes,
        "enabled": s.enabled,
        "notify_new": s.notify_new,
        "notify_drop_pct": s.notify_drop_pct,
        "baseline_sources": s.baseline_sources,
        "next_run_at": iso(s.next_run_at),
        "last_run_at": iso(s.last_run_at),
        "last_status": s.last_status,
        "last_error": s.last_error,
    }


def watch(w: Watch, item: Listing | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": w.id,
        "listing_id": w.listing_id,
        "target_price": money(w.target_price),
        "drop_pct": w.drop_pct,
        "interval_minutes": w.interval_minutes,
        "enabled": w.enabled,
        "next_run_at": iso(w.next_run_at),
        "last_run_at": iso(w.last_run_at),
        "last_status": w.last_status,
        "last_error": w.last_error,
    }
    if item is not None:
        out["listing"] = listing(item)
    return out


def source(s: Source, state: SourceState) -> dict[str, Any]:
    return {
        "id": s.info.id,
        "name": s.info.name,
        "kind": s.info.kind,
        "experimental": s.info.experimental,
        "notes": s.info.notes,
        "paused_until": iso(state.paused_until),
        "failures": state.failures,
        "last_ok_at": iso(state.last_ok_at),
        "last_error": state.last_error,
        "last_error_at": iso(state.last_error_at),
    }


def event(e: Event) -> dict[str, Any]:
    return {
        "id": e.id,
        "at": iso(e.at),
        "kind": e.kind,
        "listing_id": e.listing_id,
        "search_id": e.search_id,
        "text": e.text,
        "delivered": e.delivered,
        "error": e.error,
    }
