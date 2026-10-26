"""Use cases: search, saved searches, watched listings, source health, notifications.

This is the only module that combines sources, storage and notifications; the
API, the web UI, the scheduler and MCP all go through it.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, TypeVar

from sniflot import scoring
from sniflot.config import Settings
from sniflot.normalize import dedupe, keep, parse_words
from sniflot.notify import messages
from sniflot.notify.base import Notifier, NotifyError
from sniflot.repo import Listing, Repo, Search, SourceState, Watch, utcnow
from sniflot.sources.base import (
    BlockedError,
    Condition,
    NotFoundError,
    Offer,
    SearchParams,
    Sort,
    Source,
    SourceError,
)
from sniflot.sources.registry import source_for_url

log = logging.getLogger(__name__)
T = TypeVar("T")

MAX_PAUSE = timedelta(hours=12)
MAX_ITEMS_PER_MESSAGE = 10
MAX_DROP_MESSAGES = 10
DISPLAY_SORTS = ("relevance", "price_asc", "price_desc", "newest", "deal")


class SourcePaused(SourceError):
    def __init__(self, source: str, until: datetime) -> None:
        super().__init__(f"{source}: пауза до {until:%H:%M} UTC после блокировки")
        self.until = until


class UnsupportedURL(ValueError):
    pass


@dataclass
class ResultRow:
    listing: Listing
    peer: scoring.PeerScore
    history: float | None = None
    is_new: bool = False


@dataclass
class SearchOutcome:
    rows: list[ResultRow]
    errors: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    source_urls: dict[str, str] = field(default_factory=dict)


@dataclass
class RunReport:
    status: str
    new: int = 0
    drops: int = 0
    errors: dict[str, str] = field(default_factory=dict)


def sort_rows(rows: list[ResultRow], sort: str) -> list[ResultRow]:
    big = 1 << 62
    if sort == "price_asc":
        return sorted(rows, key=lambda r: r.listing.price if r.listing.price is not None else big)
    if sort == "price_desc":
        return sorted(rows, key=lambda r: -(r.listing.price or -1))
    if sort == "newest":
        return sorted(
            rows,
            key=lambda r: (r.listing.published_at or r.listing.first_seen_at).timestamp(),
            reverse=True,
        )
    if sort == "deal":
        # Suspiciously cheap rows go last: usually an accessory, a fake or a scam.
        return sorted(
            rows,
            key=lambda r: (
                not r.peer.suspicious,
                r.peer.score if r.peer.score is not None else -big,
            ),
            reverse=True,
        )
    return rows


class Service:
    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        sources: dict[str, Source],
        notifier: Notifier,
        *,
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
        unavailable: dict[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.unavailable = unavailable or {}
        self.repo = repo
        self.sources = sources
        self.notifier = notifier
        self.clock = clock
        self._rng = rng or random.Random()
        self._running: set[str] = set()

    async def aclose(self) -> None:
        for source in self.sources.values():
            await source.aclose()

    # --- source health ------------------------------------------------------

    def source_states(self) -> list[tuple[Source, SourceState]]:
        return [(s, self.repo.source_state(sid)) for sid, s in self.sources.items()]

    async def _call(self, source: Source, call: Callable[[], Awaitable[T]]) -> T:
        sid = source.info.id
        now = self.clock()
        state = self.repo.source_state(sid)
        if state.paused(now):
            assert state.paused_until is not None
            raise SourcePaused(sid, state.paused_until)
        try:
            result = await call()
        except NotFoundError:
            self._mark_ok(state, now)
            raise
        except BlockedError as exc:
            state.failures += 1
            pause = timedelta(seconds=source.info.block_pause * 2 ** (state.failures - 1))
            if exc.retry_after:
                pause = max(pause, timedelta(seconds=exc.retry_after))
            pause = min(pause, MAX_PAUSE)
            state.paused_until = now + pause
            state.last_error, state.last_error_at = str(exc), now
            self.repo.save_source_state(state)
            log.warning("%s blocked: %s; pausing for %s", sid, exc, pause)
            await self.notify(
                "source_blocked",
                messages.source_blocked(
                    source.info.name, str(exc), int(pause.total_seconds() // 60)
                ),
            )
            raise
        except SourceError as exc:
            state.last_error, state.last_error_at = str(exc), now
            self.repo.save_source_state(state)
            raise
        self._mark_ok(state, now)
        return result

    def _mark_ok(self, state: SourceState, now: datetime) -> None:
        state.failures, state.paused_until, state.last_ok_at = 0, None, now
        self.repo.save_source_state(state)

    def resume_source(self, source_id: str) -> None:
        state = self.repo.source_state(source_id)
        state.paused_until, state.failures = None, 0
        self.repo.save_source_state(state)

    # --- notifications ------------------------------------------------------

    async def notify(
        self, kind: str, text: str, listing_id: int | None = None, search_id: int | None = None
    ) -> None:
        event_id = self.repo.add_event(kind, text, self.clock(), listing_id, search_id)
        try:
            await self.notifier.send(text)
        except NotifyError as exc:
            log.warning("notification failed: %s", exc)
            self.repo.mark_event(event_id, False, str(exc))
        else:
            self.repo.mark_event(event_id, True)

    # --- search -------------------------------------------------------------

    async def search(
        self,
        params: SearchParams,
        source_ids: Sequence[str] | None = None,
        exclude: Sequence[str] = (),
        display_sort: str | None = None,
    ) -> SearchOutcome:
        chosen = [self.sources[s] for s in (source_ids or self.sources) if s in self.sources]
        outcome = SearchOutcome(rows=[])
        if not chosen:
            outcome.errors["*"] = "нет включённых источников"
            return outcome

        results = await asyncio.gather(
            *(self._call(s, lambda s=s: s.search(params)) for s in chosen),  # type: ignore[misc]
            return_exceptions=True,
        )
        offers: list[Offer] = []
        for source, result in zip(chosen, results, strict=True):
            if isinstance(result, SourceError):
                outcome.errors[source.info.id] = str(result)
            elif isinstance(result, BaseException):
                log.exception("unexpected error from %s", source.info.id, exc_info=result)
                outcome.errors[source.info.id] = f"внутренняя ошибка: {result!r}"
            else:
                outcome.source_urls[source.info.id] = result.source_url
                outcome.warnings.extend(f"{source.info.name}: {w}" for w in result.warnings)
                offers.extend(result.offers)

        excluded = parse_words(exclude)
        offers = dedupe(
            o
            for o in offers
            if keep(
                o,
                min_price=params.min_price,
                max_price=params.max_price,
                exclude=excluded,
                condition=params.condition,
            )
        )
        now = self.clock()
        listings = [self.repo.upsert_offer(o, now).listing for o in offers]
        outcome.rows = self.score(listings)
        outcome.rows = sort_rows(outcome.rows, display_sort or str(params.sort))
        return outcome

    def score(self, listings: list[Listing]) -> list[ResultRow]:
        peers = scoring.peer_scores([item.price for item in listings])
        histories = self.repo.histories([item.id for item in listings])
        return [
            ResultRow(item, peer, scoring.history_score(item.price, histories.get(item.id, [])))
            for item, peer in zip(listings, peers, strict=True)
        ]

    # --- saved searches -----------------------------------------------------

    def create_search(self, **fields: Any) -> Search:
        fields.setdefault("interval_minutes", self.settings.default_search_interval)
        fields.setdefault("sources", list(self.sources))
        fields.setdefault("name", fields.get("query", "")[:60])
        fields["exclude_words"] = parse_words(fields.get("exclude_words"))
        return self.repo.create_search(self.clock(), **fields)

    def search_results(self, search: Search, sort: str = "newest") -> list[ResultRow]:
        hits = self.repo.search_listings(search.id)
        rows = self.score([listing for listing, _ in hits])
        return sort_rows(rows, sort)

    def _next_run(self, now: datetime, minutes: int) -> datetime:
        jitter = self._rng.uniform(0.9, 1.1)
        return now + timedelta(minutes=max(1, minutes) * jitter)

    @staticmethod
    def params_for(search: Search) -> SearchParams:
        return SearchParams(
            query=search.query,
            min_price=search.min_price,
            max_price=search.max_price,
            region=search.region or None,
            condition=Condition(search.condition),
            sort=Sort(search.sort) if search.sort in Sort.__members__.values() else Sort.NEWEST,
            pages=search.pages,
        )

    async def run_search(self, search_id: int) -> RunReport:
        key = f"search:{search_id}"
        if key in self._running:
            return RunReport("busy")
        self._running.add(key)
        try:
            return await self._run_search(search_id)
        finally:
            self._running.discard(key)

    async def _run_search(self, search_id: int) -> RunReport:
        search = self.repo.get_search(search_id)
        if search is None:
            return RunReport("missing")
        outcome = await self.search(
            self.params_for(search), search.sources, search.exclude_words, "relevance"
        )
        now = self.clock()
        report = RunReport("ok", errors=outcome.errors)
        all_failed = bool(outcome.errors) and len(outcome.errors) >= len(
            [s for s in search.sources if s in self.sources] or [None]
        )
        # Each source gets its own baseline: its first successful run only records
        # what exists. Otherwise a source that was paused (or added) later would
        # report its whole result list as "new".
        known = set(search.baseline_sources)
        new_items: list[Listing] = []
        drops: list[tuple[Listing, int]] = []
        for row in outcome.rows:
            first, last_price = self.repo.record_hit(
                search.id, row.listing.id, row.listing.price, now
            )
            if row.listing.source not in known:
                continue
            if first:
                new_items.append(row.listing)
            elif (
                last_price
                and row.listing.price
                and row.listing.price <= last_price * (1 - search.notify_drop_pct / 100)
            ):
                drops.append((row.listing, last_price))

        if search.notify_new and new_items:
            report.new = len(new_items)
            shown = new_items[:MAX_ITEMS_PER_MESSAGE]
            await self.notify(
                "new",
                messages.new_listings(search.name, shown, len(new_items) - len(shown)),
                search_id=search.id,
            )
        if search.notify_drop_pct > 0:
            for listing, old in drops[:MAX_DROP_MESSAGES]:
                report.drops += 1
                await self.notify(
                    "price_drop",
                    messages.price_drop(
                        listing, old, search.name, scoring.suspicious_drop(old, listing.price)
                    ),
                    listing_id=listing.id,
                    search_id=search.id,
                )

        if all_failed:
            report.status = "error"
        elif outcome.errors:
            report.status = "partial"
        error_text = "; ".join(f"{k}: {v}" for k, v in outcome.errors.items()) or None
        self.repo.update_search(
            search.id,
            last_run_at=now,
            last_status=report.status,
            last_error=error_text,
            next_run_at=self._next_run(now, search.interval_minutes),
            baseline_sources=sorted(known | set(outcome.source_urls)),
        )
        return report

    # --- watched listings ---------------------------------------------------

    async def add_watch_by_url(
        self,
        url: str,
        target_price: int | None = None,
        drop_pct: float = 1.0,
        interval_minutes: int | None = None,
    ) -> Watch:
        found = source_for_url(self.sources, url.strip())
        if found is None:
            raise UnsupportedURL(
                "ссылка не распознана ни одним включённым источником: "
                + ", ".join(s.info.name for s in self.sources.values())
            )
        source, _ = found
        offer = await self._call(source, lambda: source.fetch(url.strip()))
        listing = self.repo.upsert_offer(offer, self.clock()).listing
        return self.add_watch(listing.id, target_price, drop_pct, interval_minutes)

    def add_watch(
        self,
        listing_id: int,
        target_price: int | None = None,
        drop_pct: float = 1.0,
        interval_minutes: int | None = None,
    ) -> Watch:
        now = self.clock()
        listing = self.repo.get_listing(listing_id)
        if listing is None:
            raise KeyError(listing_id)
        return self.repo.create_watch(
            listing_id,
            now,
            last_price=listing.price,
            last_available=listing.available,
            target_price=target_price,
            drop_pct=drop_pct,
            interval_minutes=interval_minutes or self.settings.default_watch_interval,
            next_run_at=self._next_run(
                now, interval_minutes or self.settings.default_watch_interval
            ),
        )

    async def check_watch(self, watch_id: int) -> str:
        key = f"watch:{watch_id}"
        if key in self._running:
            return "busy"
        self._running.add(key)
        try:
            return await self._check_watch(watch_id)
        finally:
            self._running.discard(key)

    async def _check_watch(self, watch_id: int) -> str:
        watch = self.repo.get_watch(watch_id)
        listing = self.repo.get_listing(watch.listing_id) if watch else None
        if watch is None or listing is None:
            return "missing"
        now = self.clock()
        next_run = self._next_run(now, watch.interval_minutes)
        source = self.sources.get(listing.source)
        status, error = "ok", None
        changes: dict[str, Any] = {}
        if source is None:
            status, error = "error", f"источник {listing.source} выключен"
        else:
            try:
                offer = await self._call(source, lambda: source.fetch(listing.url))
            except NotFoundError:
                status = "gone"
                if listing.status != "gone":
                    self.repo.mark_gone(listing.id, now)
                    await self.notify("gone", messages.gone(listing), listing_id=listing.id)
            except SourcePaused as exc:
                status, error = "paused", str(exc)
                next_run = max(next_run, exc.until)
            except SourceError as exc:
                status, error = "error", str(exc)
            else:
                fresh = self.repo.upsert_offer(offer, now).listing
                await self._compare(watch, fresh)
                changes = {"last_price": fresh.price, "last_available": fresh.available}
        self.repo.update_watch(
            watch.id,
            last_run_at=now,
            last_status=status,
            last_error=error,
            next_run_at=next_run,
            **changes,
        )
        return status

    async def _compare(self, watch: Watch, listing: Listing) -> None:
        # Compare with what this watch saw last time, not with the listing row:
        # a saved search may have refreshed the row in between.
        old, new = watch.last_price, listing.price
        if watch.last_available is True and listing.available is False:
            await self.notify("out_of_stock", messages.out_of_stock(listing), listing_id=listing.id)
        elif watch.last_available is False and listing.available:
            await self.notify(
                "back_in_stock", messages.back_in_stock(listing), listing_id=listing.id
            )
        if not old or not new or new >= old:
            return
        if watch.target_price and new <= watch.target_price < old:
            await self.notify(
                "target",
                messages.target_reached(listing, watch.target_price),
                listing_id=listing.id,
            )
        elif (old - new) / old * 100 >= watch.drop_pct:
            await self.notify(
                "price_drop",
                messages.price_drop(listing, old, suspicious=scoring.suspicious_drop(old, new)),
                listing_id=listing.id,
            )

    # --- scheduler entry point ----------------------------------------------

    async def run_due(self) -> int:
        now = self.clock()
        done = 0
        for search in self.repo.due_searches(now):
            await self.run_search(search.id)
            done += 1
        for watch in self.repo.due_watches(now):
            await self.check_watch(watch.id)
            done += 1
        return done
