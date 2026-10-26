from __future__ import annotations

import pytest

from sniflot.services import Service, SourcePaused
from sniflot.sources.base import BlockedError, SearchParams, SourceError

from .conftest import Clock, FakeNotifier, FakeSource

pytestmark = pytest.mark.anyio


async def test_search_filters_dedupes_sorts_and_persists(
    service: Service, source: FakeSource
) -> None:
    source.offers = [
        source.offer("1", 3000, "Наушники A"),
        source.offer("2", 1000, "Наушники B"),
        source.offer("2", 1000, "Наушники B"),
        source.offer("3", 2000, "Чехол для наушников"),
        source.offer("4", 9000, "Наушники дорогие"),
    ]
    outcome = await service.search(
        SearchParams("наушники", max_price=5000), exclude=["чехол"], display_sort="price_asc"
    )
    assert [r.listing.external_id for r in outcome.rows] == ["2", "1"]
    assert outcome.errors == {}
    assert service.repo.find_listing("fake", "1") is not None


async def test_search_reports_source_errors_without_failing(
    service: Service, source: FakeSource
) -> None:
    source.error = SourceError("timeout")
    outcome = await service.search(SearchParams("x"))
    assert outcome.rows == [] and outcome.errors == {"fake": "timeout"}


async def test_saved_search_baseline_then_new_and_drop(
    service: Service, source: FakeSource, notifier: FakeNotifier, clock: Clock
) -> None:
    search = service.create_search(query="наушники", notify_drop_pct=10)
    source.offers = [source.offer("1", 3000), source.offer("2", 2000)]

    report = await service.run_search(search.id)
    assert report.status == "ok" and report.new == 0
    assert notifier.sent == []  # the first run only records a baseline

    clock.advance(hours=1)
    source.offers = [source.offer("1", 2500), source.offer("2", 1950), source.offer("3", 1000)]
    report = await service.run_search(search.id)
    assert report.new == 1 and report.drops == 1  # 2: −2.5% is below the 10% threshold
    assert "новых объявлений — 1" in notifier.sent[0] and "Товар 3" in notifier.sent[0]
    assert "📉" in notifier.sent[1] and "Товар 1" in notifier.sent[1]

    saved = service.repo.get_search(search.id)
    assert saved is not None and saved.baseline_done and saved.next_run_at > clock.now
    assert len(service.search_results(saved)) == 3


async def test_failed_first_run_does_not_set_baseline(
    service: Service, source: FakeSource, notifier: FakeNotifier
) -> None:
    search = service.create_search(query="x")
    source.error = SourceError("down")
    report = await service.run_search(search.id)
    assert report.status == "error"
    saved = service.repo.get_search(search.id)
    assert saved is not None and not saved.baseline_done and saved.last_error == "fake: down"
    source.error = None
    source.offers = [source.offer("1", 100)]
    await service.run_search(search.id)
    assert notifier.sent == []


async def test_block_pauses_source_and_backs_off(
    service: Service, source: FakeSource, notifier: FakeNotifier, clock: Clock
) -> None:
    source.error = BlockedError("HTTP 429")
    outcome = await service.search(SearchParams("x"))
    assert "429" in outcome.errors["fake"]
    state = service.repo.source_state("fake")
    assert state.paused(clock.now) and state.failures == 1
    assert "ограничил доступ" in notifier.sent[0] and "10 мин" in notifier.sent[0]

    calls = source.calls
    outcome = await service.search(SearchParams("x"))
    assert source.calls == calls  # paused: no request at all
    assert "пауза" in outcome.errors["fake"]

    clock.advance(minutes=11)
    await service.search(SearchParams("x"))
    state = service.repo.source_state("fake")
    assert state.failures == 2
    assert state.paused_until is not None
    assert (state.paused_until - clock.now).total_seconds() == 1200  # doubled

    source.error = None
    clock.advance(minutes=21)
    await service.search(SearchParams("x"))
    state = service.repo.source_state("fake")
    assert state.failures == 0 and not state.paused(clock.now)


async def test_watch_by_url_drop_target_stock_and_gone(
    service: Service, source: FakeSource, notifier: FakeNotifier, clock: Clock
) -> None:
    source.items["7"] = source.offer("7", 10000, available=True)
    watch = await service.add_watch_by_url(
        "https://fake.example/item/7", target_price=8000_00, drop_pct=5
    )
    listing_id = watch.listing_id

    source.items["7"] = source.offer("7", 9700, available=True)  # −3%: below threshold
    assert await service.check_watch(watch.id) == "ok"
    assert notifier.sent == []

    source.items["7"] = source.offer("7", 9000, available=True)  # −7.2% vs last check
    await service.check_watch(watch.id)
    assert "📉" in notifier.sent[-1]

    source.items["7"] = source.offer("7", 7900, available=True)
    await service.check_watch(watch.id)
    assert "🎯" in notifier.sent[-1]

    source.items["7"] = source.offer("7", 7900, available=False)
    await service.check_watch(watch.id)
    assert "Нет в наличии" in notifier.sent[-1]

    source.items["7"] = source.offer("7", 7900, available=True)
    await service.check_watch(watch.id)
    assert "Снова в наличии" in notifier.sent[-1]

    del source.items["7"]
    assert await service.check_watch(watch.id) == "gone"
    assert "снято" in notifier.sent[-1]
    count = len(notifier.sent)
    assert await service.check_watch(watch.id) == "gone"
    assert len(notifier.sent) == count  # "gone" is reported once
    listing = service.repo.get_listing(listing_id)
    assert listing is not None and listing.status == "gone"


async def test_watch_ignores_changes_made_by_searches(
    service: Service, source: FakeSource, notifier: FakeNotifier
) -> None:
    source.items["7"] = source.offer("7", 10000)
    watch = await service.add_watch_by_url("https://fake.example/item/7", drop_pct=5)
    # A saved search refreshes the same listing with a lower price first...
    source.offers = [source.offer("7", 9000)]
    await service.search(SearchParams("x"))
    # ...the watch still notices the drop relative to its own last check.
    source.items["7"] = source.offer("7", 9000)
    await service.check_watch(watch.id)
    assert any("📉" in m for m in notifier.sent)


async def test_watch_on_paused_source_is_rescheduled(
    service: Service, source: FakeSource, clock: Clock
) -> None:
    source.items["7"] = source.offer("7", 100)
    watch = await service.add_watch_by_url("https://fake.example/item/7")
    source.error = BlockedError("captcha")
    await service.check_watch(watch.id)
    assert await service.check_watch(watch.id) == "paused"
    saved = service.repo.get_watch(watch.id)
    state = service.repo.source_state("fake")
    assert saved is not None and state.paused_until is not None
    assert saved.next_run_at >= state.paused_until


async def test_unsupported_url(service: Service) -> None:
    with pytest.raises(ValueError, match="не распознана"):
        await service.add_watch_by_url("https://example.org/x")


async def test_run_due_runs_scheduled_tasks(
    service: Service, source: FakeSource, clock: Clock
) -> None:
    service.create_search(query="x")
    source.items["7"] = source.offer("7", 100)
    await service.add_watch_by_url("https://fake.example/item/7", interval_minutes=60)
    assert await service.run_due() == 1  # the search is due now, the watch in ~1h
    clock.advance(hours=2)
    assert await service.run_due() == 2


async def test_paused_error_type() -> None:
    from datetime import UTC, datetime

    assert isinstance(SourcePaused("x", datetime.now(UTC)), SourceError)


def test_deal_sort_puts_suspicious_last(service: Service, source: FakeSource) -> None:
    from sniflot.repo import utcnow
    from sniflot.services import sort_rows

    listings = [
        service.repo.upsert_offer(source.offer(str(i), p), utcnow()).listing
        for i, p in enumerate([1000, 1000, 1000, 1000, 900, 300])
    ]
    rows = sort_rows(service.score(listings), "deal")
    assert [r.listing.price for r in rows][:1] == [90000]
    assert rows[-1].listing.price == 30000 and rows[-1].peer.suspicious


async def test_baseline_is_per_source(repo, notifier: FakeNotifier, clock: Clock) -> None:
    import random
    from pathlib import Path

    from sniflot.config import Settings

    a, b = FakeSource("a"), FakeSource("b")
    service = Service(
        Settings(db_path=Path(":memory:")),
        repo,
        {"a": a, "b": b},
        notifier,
        clock=clock,
        rng=random.Random(1),
    )
    search = service.create_search(query="x")
    a.offers = [a.offer("1", 100)]
    b.error = BlockedError("captcha")
    report = await service.run_search(search.id)
    assert report.status == "partial"
    notifier.sent.clear()

    # b comes back with a long list: that is its baseline, not 3 new listings.
    clock.advance(hours=2)
    b.error = None
    b.offers = [b.offer(str(i), 100) for i in range(3)]
    a.offers = [a.offer("1", 100), a.offer("2", 100)]
    report = await service.run_search(search.id)
    assert report.new == 1 and "Товар 2" in notifier.sent[-1]
    saved = service.repo.get_search(search.id)
    assert saved is not None and saved.baseline_sources == ["a", "b"]

    clock.advance(hours=1)
    b.offers.append(b.offer("9", 100))
    report = await service.run_search(search.id)
    assert report.new == 1 and "Товар 9" in notifier.sent[-1]
