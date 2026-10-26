"""REST API, mounted at /api/v1. Prices in requests and responses are roubles."""

from __future__ import annotations

import csv
import io
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from sniflot import serialize
from sniflot.normalize import parse_words
from sniflot.services import DISPLAY_SORTS, Service, SourcePaused, UnsupportedURL
from sniflot.sources.base import Condition, SearchParams, Sort, SourceError

router = APIRouter(prefix="/api/v1")


def get_service(request: Request) -> Service:
    service: Service = request.app.state.service
    return service


Svc = Annotated[Service, Depends(get_service)]


class SearchIn(BaseModel):
    query: str = Field(min_length=1)
    name: str | None = None
    sources: list[str] | None = None
    min_price: int | None = Field(default=None, ge=0)
    max_price: int | None = Field(default=None, ge=0)
    region: str | None = None
    condition: Condition = Condition.ANY
    sort: Sort = Sort.NEWEST
    exclude_words: list[str] = []
    pages: int = Field(default=1, ge=1, le=5)
    interval_minutes: int | None = Field(default=None, ge=5)
    enabled: bool = True
    notify_new: bool = True
    notify_drop_pct: float = Field(default=5, ge=0, le=100)


class SearchPatch(BaseModel):
    name: str | None = None
    query: str | None = None
    sources: list[str] | None = None
    min_price: int | None = Field(default=None, ge=0)
    max_price: int | None = Field(default=None, ge=0)
    region: str | None = None
    condition: Condition | None = None
    sort: Sort | None = None
    exclude_words: list[str] | None = None
    pages: int | None = Field(default=None, ge=1, le=5)
    interval_minutes: int | None = Field(default=None, ge=5)
    enabled: bool | None = None
    notify_new: bool | None = None
    notify_drop_pct: float | None = Field(default=None, ge=0, le=100)


class WatchIn(BaseModel):
    url: str | None = None
    listing_id: int | None = None
    target_price: int | None = Field(default=None, ge=0)
    drop_pct: float = Field(default=1, ge=0, le=100)
    interval_minutes: int | None = Field(default=None, ge=5)


class WatchPatch(BaseModel):
    target_price: int | None = Field(default=None, ge=0)
    drop_pct: float | None = Field(default=None, ge=0, le=100)
    interval_minutes: int | None = Field(default=None, ge=5)
    enabled: bool | None = None


class FavoriteIn(BaseModel):
    favorite: bool = True


def _known_sources(service: Service, ids: list[str] | None) -> list[str] | None:
    if ids is None:
        return None
    unknown = [s for s in ids if s not in service.sources]
    if unknown:
        raise HTTPException(422, f"unknown or disabled sources: {', '.join(unknown)}")
    return ids


@router.get("/health")
def health(service: Svc) -> dict[str, Any]:
    return {
        "status": "ok",
        "sources": [serialize.source(s, st) for s, st in service.source_states()],
        "notifier": service.notifier.name,
    }


@router.get("/sources")
def sources(service: Svc) -> list[dict[str, Any]]:
    return [serialize.source(s, st) for s, st in service.source_states()]


@router.post("/sources/{source_id}/resume")
def resume_source(source_id: str, service: Svc) -> dict[str, str]:
    if source_id not in service.sources:
        raise HTTPException(404, "unknown source")
    service.resume_source(source_id)
    return {"status": "resumed"}


@router.get("/search")
async def search(
    service: Svc,
    q: Annotated[str, Query(min_length=1)],
    sources: Annotated[list[str] | None, Query()] = None,
    min_price: int | None = None,
    max_price: int | None = None,
    region: str | None = None,
    condition: Condition = Condition.ANY,
    sort: Annotated[str, Query(pattern="|".join(DISPLAY_SORTS))] = "relevance",
    exclude: Annotated[list[str] | None, Query()] = None,
    pages: Annotated[int, Query(ge=1, le=5)] = 1,
) -> dict[str, Any]:
    source_sort = Sort(sort) if sort in Sort.__members__.values() else Sort.RELEVANCE
    params = SearchParams(q, min_price, max_price, region, condition, source_sort, pages)
    outcome = await service.search(
        params, _known_sources(service, sources), exclude or [], display_sort=sort
    )
    return {
        "results": [serialize.row(r) for r in outcome.rows],
        "errors": outcome.errors,
        "warnings": outcome.warnings,
        "source_urls": outcome.source_urls,
    }


@router.get("/searches")
def list_searches(service: Svc) -> list[dict[str, Any]]:
    return [serialize.search(s) for s in service.repo.searches()]


@router.post("/searches", status_code=201)
def create_search(body: SearchIn, service: Svc) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True)
    fields["sources"] = _known_sources(service, body.sources) or list(service.sources)
    return serialize.search(service.create_search(**fields))


def _search_or_404(service: Service, search_id: int) -> Any:
    found = service.repo.get_search(search_id)
    if found is None:
        raise HTTPException(404, "search not found")
    return found


@router.get("/searches/{search_id}")
def get_search(search_id: int, service: Svc) -> dict[str, Any]:
    return serialize.search(_search_or_404(service, search_id))


@router.patch("/searches/{search_id}")
def patch_search(search_id: int, body: SearchPatch, service: Svc) -> dict[str, Any]:
    _search_or_404(service, search_id)
    fields = body.model_dump(exclude_unset=True)
    if "sources" in fields:
        _known_sources(service, fields["sources"])
    if "exclude_words" in fields:
        fields["exclude_words"] = parse_words(fields["exclude_words"])
    service.repo.update_search(search_id, **fields)
    return serialize.search(_search_or_404(service, search_id))


@router.delete("/searches/{search_id}", status_code=204)
def delete_search(search_id: int, service: Svc) -> Response:
    _search_or_404(service, search_id)
    service.repo.delete_search(search_id)
    return Response(status_code=204)


@router.post("/searches/{search_id}/run")
async def run_search(search_id: int, service: Svc) -> dict[str, Any]:
    _search_or_404(service, search_id)
    report = await service.run_search(search_id)
    return {
        "status": report.status,
        "new": report.new,
        "drops": report.drops,
        "errors": report.errors,
    }


@router.get("/searches/{search_id}/results")
def search_results(
    search_id: int,
    service: Svc,
    sort: Annotated[str, Query(pattern="|".join(DISPLAY_SORTS))] = "newest",
) -> list[dict[str, Any]]:
    found = _search_or_404(service, search_id)
    return [serialize.row(r) for r in service.search_results(found, sort)]


@router.get("/watches")
def list_watches(service: Svc) -> list[dict[str, Any]]:
    return [serialize.watch(w, item) for w, item in service.repo.watches()]


@router.post("/watches", status_code=201)
async def create_watch(body: WatchIn, service: Svc) -> dict[str, Any]:
    target = body.target_price * 100 if body.target_price is not None else None
    if body.url:
        try:
            watch = await service.add_watch_by_url(
                body.url, target, body.drop_pct, body.interval_minutes
            )
        except UnsupportedURL as exc:
            raise HTTPException(422, str(exc)) from exc
        except SourcePaused as exc:
            raise HTTPException(503, str(exc)) from exc
        except SourceError as exc:
            raise HTTPException(502, str(exc)) from exc
    elif body.listing_id is not None:
        if service.repo.get_listing(body.listing_id) is None:
            raise HTTPException(404, "listing not found")
        watch = service.add_watch(body.listing_id, target, body.drop_pct, body.interval_minutes)
    else:
        raise HTTPException(422, "url or listing_id is required")
    return serialize.watch(watch, service.repo.get_listing(watch.listing_id))


@router.patch("/watches/{watch_id}")
def patch_watch(watch_id: int, body: WatchPatch, service: Svc) -> dict[str, Any]:
    if service.repo.get_watch(watch_id) is None:
        raise HTTPException(404, "watch not found")
    fields = body.model_dump(exclude_unset=True)
    if fields.get("target_price") is not None:
        fields["target_price"] *= 100
    service.repo.update_watch(watch_id, **fields)
    watch = service.repo.get_watch(watch_id)
    assert watch is not None
    return serialize.watch(watch, service.repo.get_listing(watch.listing_id))


@router.delete("/watches/{watch_id}", status_code=204)
def delete_watch(watch_id: int, service: Svc) -> Response:
    if service.repo.get_watch(watch_id) is None:
        raise HTTPException(404, "watch not found")
    service.repo.delete_watch(watch_id)
    return Response(status_code=204)


@router.post("/watches/{watch_id}/check")
async def check_watch(watch_id: int, service: Svc) -> dict[str, str]:
    if service.repo.get_watch(watch_id) is None:
        raise HTTPException(404, "watch not found")
    return {"status": await service.check_watch(watch_id)}


def _listing_or_404(service: Service, listing_id: int) -> Any:
    found = service.repo.get_listing(listing_id)
    if found is None:
        raise HTTPException(404, "listing not found")
    return found


@router.get("/listings/favorites")
def favorites(service: Svc) -> list[dict[str, Any]]:
    return [serialize.listing(item) for item in service.repo.favorites()]


@router.get("/listings/{listing_id}")
def get_listing(listing_id: int, service: Svc) -> dict[str, Any]:
    item = _listing_or_404(service, listing_id)
    watch = service.repo.watch_for_listing(listing_id)
    return {
        **serialize.listing(item),
        "history": [serialize.point(p) for p in service.repo.history(listing_id)],
        "watch": serialize.watch(watch) if watch else None,
    }


@router.post("/listings/{listing_id}/favorite")
def set_favorite(listing_id: int, body: FavoriteIn, service: Svc) -> dict[str, Any]:
    _listing_or_404(service, listing_id)
    service.repo.set_favorite(listing_id, body.favorite)
    return serialize.listing(_listing_or_404(service, listing_id))


@router.get("/listings/{listing_id}/history.json")
def history_json(listing_id: int, service: Svc) -> Response:
    item = _listing_or_404(service, listing_id)
    data = {
        "listing": serialize.listing(item),
        "history": [serialize.point(p) for p in service.repo.history(listing_id)],
    }
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="listing-{listing_id}.json"'},
    )


@router.get("/listings/{listing_id}/history.csv")
def history_csv(listing_id: int, service: Svc) -> Response:
    _listing_or_404(service, listing_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["at", "price_rub", "available"])
    for p in service.repo.history(listing_id):
        price = serialize.money(p.price)
        writer.writerow([p.at.isoformat(), "" if price is None else price, p.available])
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="listing-{listing_id}.csv"'},
    )


@router.get("/events")
def events(service: Svc, limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[dict[str, Any]]:
    return [serialize.event(e) for e in service.repo.events(limit)]


@router.post("/notify/test")
async def notify_test(service: Svc) -> dict[str, Any]:
    await service.notify("test", "🔔 sniflot: тестовое уведомление")
    last = service.repo.events(1)[0]
    return {"delivered": last.delivered, "error": last.error, "channel": service.notifier.name}
