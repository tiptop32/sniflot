"""Server-rendered web UI (Jinja2). Plain HTML forms, no client-side framework."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from sniflot import charts
from sniflot.normalize import parse_words
from sniflot.notify.messages import rub
from sniflot.services import DISPLAY_SORTS, Service, UnsupportedURL
from sniflot.sources.base import Condition, SearchParams, Sort, SourceError

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

SORT_LABELS = {
    "relevance": "по релевантности",
    "price_asc": "дешевле",
    "price_desc": "дороже",
    "newest": "новые",
    "deal": "выгоднее",
}
STATUS_LABELS = {
    "ok": "ок",
    "partial": "частично",
    "error": "ошибка",
    "paused": "пауза",
    "gone": "снято",
    "busy": "выполняется",
}


def _tz(request: Request) -> tzinfo:
    try:
        return ZoneInfo(request.app.state.settings.tz)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _service(request: Request) -> Service:
    service: Service = request.app.state.service
    return service


def _render(request: Request, name: str, **context: Any) -> HTMLResponse:
    tz = _tz(request)

    def dt(value: datetime | None, fmt: str = "%d.%m %H:%M") -> str:
        return value.astimezone(tz).strftime(fmt) if value else "—"

    service = _service(request)
    return templates.TemplateResponse(
        request,
        name,
        {
            "rub": rub,
            "dt": dt,
            "sort_labels": SORT_LABELS,
            "status_labels": STATUS_LABELS,
            "sources": service.sources,
            "msg": request.query_params.get("msg"),
            "err": request.query_params.get("err"),
            **context,
        },
    )


def _back(path: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


def _int(value: Any) -> int | None:
    text = str(value or "").replace(" ", "").replace(" ", "").strip()
    return int(float(text.replace(",", "."))) if text else None


def _float(value: Any, default: float) -> float:
    text = str(value or "").replace(",", ".").strip()
    return float(text) if text else default


def _search_fields(form: FormData, service: Service) -> dict[str, Any]:
    sources = [s for s in form.getlist("sources") if isinstance(s, str) and s in service.sources]
    sort = str(form.get("sort") or "newest")
    return {
        "name": str(form.get("name") or form.get("query") or "")[:80].strip(),
        "query": str(form.get("query") or "").strip(),
        "sources": sources or list(service.sources),
        "min_price": _int(form.get("min_price")),
        "max_price": _int(form.get("max_price")),
        "region": str(form.get("region") or "").strip() or None,
        "condition": str(form.get("condition") or "any"),
        "sort": sort if sort in Sort.__members__.values() else "newest",
        "exclude_words": parse_words(str(form.get("exclude_words") or "")),
        "pages": max(1, min(5, _int(form.get("pages")) or 1)),
        "interval_minutes": max(5, _int(form.get("interval_minutes")) or 30),
        "notify_new": form.get("notify_new") == "on",
        "notify_drop_pct": _float(form.get("notify_drop_pct"), 5),
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    service = _service(request)
    watches = [
        (w, item, charts.sparkline(service.repo.history(item.id)))
        for w, item in service.repo.watches()
    ]
    return _render(
        request,
        "dashboard.html",
        watches=watches,
        searches=service.repo.searches(),
        events=service.repo.events(15),
        states=service.source_states(),
        now=service.clock(),
    )


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request) -> HTMLResponse:
    service = _service(request)
    q = request.query_params
    query = (q.get("q") or "").strip()
    chosen = [s for s in q.getlist("sources") if s in service.sources] or list(service.sources)
    sort = str(q.get("sort") or "")
    sort = sort if sort in DISPLAY_SORTS else "relevance"
    form: dict[str, Any] = {
        "q": query,
        "sources": chosen,
        "min_price": q.get("min_price") or "",
        "max_price": q.get("max_price") or "",
        "region": q.get("region") or "",
        "condition": q.get("condition") or "any",
        "exclude": q.get("exclude") or "",
        "sort": sort,
        "view": q.get("view") or "table",
    }
    outcome = None
    if query:
        condition = form["condition"] if form["condition"] in ("any", "new", "used") else "any"
        params = SearchParams(
            query=query,
            min_price=_int(form["min_price"]),
            max_price=_int(form["max_price"]),
            region=form["region"] or None,
            condition=Condition(condition),
            sort=Sort(sort) if sort in Sort.__members__.values() else Sort.RELEVANCE,
        )
        outcome = await service.search(params, chosen, parse_words(form["exclude"]), sort)
    return _render(request, "search.html", form=form, outcome=outcome)


@router.post("/searches")
async def create_search(request: Request) -> Response:
    service = _service(request)
    fields = _search_fields(await request.form(), service)
    if not fields["query"]:
        return _back("/search", err="Пустой запрос")
    search = service.create_search(**fields)
    return _back(
        f"/searches/{search.id}", msg="Поиск сохранён; первый прогон запишет базовую линию"
    )


@router.get("/searches/{search_id}", response_class=HTMLResponse)
def search_detail(request: Request, search_id: int) -> HTMLResponse:
    service = _service(request)
    search = service.repo.get_search(search_id)
    if search is None:
        raise HTTPException(404)
    sort = request.query_params.get("sort")
    sort = sort if sort in DISPLAY_SORTS else "newest"
    view = request.query_params.get("view") or "table"
    rows = service.search_results(search, sort)
    return _render(request, "search_detail.html", search=search, rows=rows, sort=sort, view=view)


@router.post("/searches/{search_id}")
async def update_search(request: Request, search_id: int) -> Response:
    service = _service(request)
    if service.repo.get_search(search_id) is None:
        raise HTTPException(404)
    form = await request.form()
    fields = _search_fields(form, service)
    fields["enabled"] = form.get("enabled") == "on"
    service.repo.update_search(search_id, **fields)
    return _back(f"/searches/{search_id}", msg="Сохранено")


@router.post("/searches/{search_id}/run")
async def run_search(request: Request, search_id: int) -> Response:
    report = await _service(request).run_search(search_id)
    errors = "; ".join(f"{k}: {v}" for k, v in report.errors.items())
    return _back(
        f"/searches/{search_id}",
        msg=f"Готово: новых {report.new}, снижений {report.drops}"
        if report.status != "error"
        else None,
        err=errors or None,
    )


@router.post("/searches/{search_id}/delete")
def delete_search(request: Request, search_id: int) -> Response:
    _service(request).repo.delete_search(search_id)
    return _back("/", msg="Поиск удалён")


@router.get("/add", response_class=HTMLResponse)
def add_page(request: Request) -> HTMLResponse:
    return _render(request, "add.html", url=request.query_params.get("url", ""))


@router.post("/watches")
async def create_watch(request: Request) -> Response:
    service = _service(request)
    form = await request.form()
    target = _int(form.get("target_price"))
    target_kop = target * 100 if target is not None else None
    drop = _float(form.get("drop_pct"), 1)
    interval = _int(form.get("interval_minutes"))
    listing_id = _int(form.get("listing_id"))
    url = str(form.get("url") or "").strip()
    try:
        if listing_id:
            watch = service.add_watch(listing_id, target_kop, drop, interval)
        elif url:
            watch = await service.add_watch_by_url(url, target_kop, drop, interval)
        else:
            return _back("/add", err="Укажите ссылку")
    except (UnsupportedURL, SourceError) as exc:
        return RedirectResponse(f"/add?url={quote(url)}&err={quote(str(exc))}", status_code=303)
    return _back(f"/listings/{watch.listing_id}", msg="Отслеживание добавлено")


@router.post("/watches/{watch_id}")
async def update_watch(request: Request, watch_id: int) -> Response:
    service = _service(request)
    watch = service.repo.get_watch(watch_id)
    if watch is None:
        raise HTTPException(404)
    form = await request.form()
    target = _int(form.get("target_price"))
    service.repo.update_watch(
        watch_id,
        target_price=target * 100 if target is not None else None,
        drop_pct=_float(form.get("drop_pct"), 1),
        interval_minutes=max(5, _int(form.get("interval_minutes")) or watch.interval_minutes),
        enabled=form.get("enabled") == "on",
    )
    return _back(f"/listings/{watch.listing_id}", msg="Сохранено")


@router.post("/watches/{watch_id}/check")
async def check_watch(request: Request, watch_id: int) -> Response:
    service = _service(request)
    watch = service.repo.get_watch(watch_id)
    if watch is None:
        raise HTTPException(404)
    status = await service.check_watch(watch_id)
    fresh = service.repo.get_watch(watch_id)
    err = fresh.last_error if fresh else None
    return _back(
        f"/listings/{watch.listing_id}",
        msg=f"Проверено: {STATUS_LABELS.get(status, status)}",
        err=err,
    )


@router.post("/watches/{watch_id}/delete")
def delete_watch(request: Request, watch_id: int) -> Response:
    service = _service(request)
    watch = service.repo.get_watch(watch_id)
    service.repo.delete_watch(watch_id)
    return _back(f"/listings/{watch.listing_id}" if watch else "/", msg="Отслеживание удалено")


@router.get("/listings/{listing_id}", response_class=HTMLResponse)
def listing_page(request: Request, listing_id: int) -> HTMLResponse:
    service = _service(request)
    listing = service.repo.get_listing(listing_id)
    if listing is None:
        raise HTTPException(404)
    history = service.repo.history(listing_id)
    return _render(
        request,
        "listing.html",
        listing=listing,
        history=list(reversed(history)),
        chart=charts.price_chart(history, service.clock()),
        watch=service.repo.watch_for_listing(listing_id),
    )


@router.post("/listings/{listing_id}/favorite")
def toggle_favorite(request: Request, listing_id: int) -> Response:
    service = _service(request)
    listing = service.repo.get_listing(listing_id)
    if listing is None:
        raise HTTPException(404)
    service.repo.set_favorite(listing_id, not listing.favorite)
    back = request.headers.get("referer") or f"/listings/{listing_id}"
    return RedirectResponse(back, status_code=303)


@router.get("/favorites", response_class=HTMLResponse)
def favorites(request: Request) -> HTMLResponse:
    service = _service(request)
    return _render(request, "favorites.html", rows=service.score(service.repo.favorites()))


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    service = _service(request)
    base = str(request.base_url).rstrip("/")
    bookmarklet = (
        "javascript:(()=>{window.open('"
        + base
        + "/add?url='+encodeURIComponent(location.href))})()"
    )
    return _render(
        request,
        "settings.html",
        states=service.source_states(),
        notifier=service.notifier.name,
        bookmarklet=bookmarklet,
        now=service.clock(),
        settings=request.app.state.settings,
    )


@router.post("/sources/{source_id}/resume")
def resume_source(request: Request, source_id: str) -> Response:
    _service(request).resume_source(source_id)
    return _back("/settings", msg=f"Источник {source_id} снят с паузы")


@router.post("/notify/test")
async def notify_test(request: Request) -> Response:
    service = _service(request)
    await service.notify("test", "🔔 sniflot: тестовое уведомление")
    event = service.repo.events(1)[0]
    if event.delivered:
        return _back("/settings", msg=f"Отправлено через {service.notifier.name}")
    return _back("/settings", err=f"Не доставлено: {event.error}")
