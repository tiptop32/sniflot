from __future__ import annotations

from sniflot import scoring
from sniflot.normalize import dedupe, has_excluded, keep, norm_title, parse_words
from sniflot.repo import Repo
from sniflot.sources.base import Condition, Offer

from .conftest import Clock


def offer(ext: str, price: int | None, title: str = "x", **kw: object) -> Offer:
    return Offer(
        source="s", external_id=ext, url=f"https://s/{ext}", title=title, price=price, **kw
    )  # type: ignore[arg-type]


def test_norm_title_and_exclude_words() -> None:
    assert norm_title("iPhone 13, 128 ГБ — Ёлка!") == "iphone 13 128 гб елка"
    words = parse_words("Чехол, на запчасти\nкопия")
    assert words == ["чехол", "на запчасти", "копия"]
    assert has_excluded("iPhone 13 на запчасти", words)
    assert has_excluded("Чехол для iPhone", words)
    assert not has_excluded("iPhone 13 чехолы в подарок", ["чехол"])  # whole words only


def test_keep_filters_price_and_condition() -> None:
    assert keep(offer("1", 5000_00), min_price=1000, max_price=6000)
    assert not keep(offer("1", 7000_00), max_price=6000)
    assert not keep(offer("1", 500_00), min_price=1000)
    assert keep(offer("1", None), min_price=1000)  # unknown price is kept, shown as "—"
    assert not keep(offer("1", 1, condition=Condition.NEW), condition=Condition.USED)
    assert keep(offer("1", 1), condition=Condition.USED)  # unknown condition is kept


def test_dedupe() -> None:
    assert [o.price for o in dedupe([offer("1", 1), offer("1", 2), offer("2", 3)])] == [1, 3]


def test_peer_scores_and_suspicious() -> None:
    assert all(s.score is None for s in scoring.peer_scores([100, 200, 300]))
    scores = scoring.peer_scores([1000, 1000, 1000, 1000, 900, 300, None])
    assert scores[4].score == 0.1 and not scores[4].suspicious
    assert scores[5].suspicious
    assert scores[6].score is None


def test_history_score_and_drops() -> None:
    assert scoring.history_score(800, [1000, 1000, 900]) == 0.2
    assert scoring.history_score(800, [1000]) is None
    assert scoring.suspicious_drop(1000, 400)
    assert not scoring.suspicious_drop(1000, 600)
    assert scoring.drop_ratio(1000, 1100) is None


def test_upsert_records_history_only_on_change(repo: Repo, clock: Clock) -> None:
    first = repo.upsert_offer(offer("1", 1000, available=True), clock())
    assert first.created
    clock.advance(hours=1)
    same = repo.upsert_offer(offer("1", 1000, available=True), clock())
    assert not same.created and not same.price_changed
    clock.advance(hours=1)
    cheaper = repo.upsert_offer(offer("1", 900, available=True), clock())
    assert cheaper.price_changed and cheaper.old_price == 1000
    clock.advance(hours=1)
    repo.upsert_offer(offer("1", 900, available=False), clock())
    history = repo.history(first.listing.id)
    assert [(p.price, p.available) for p in history] == [(1000, True), (900, True), (900, False)]


def test_upsert_keeps_optional_facts(repo: Repo, clock: Clock) -> None:
    repo.upsert_offer(offer("1", 1000, seller="Shop", image_url="https://i"), clock())
    updated = repo.upsert_offer(offer("1", 1000), clock()).listing
    assert updated.seller == "Shop" and updated.image_url == "https://i"


def test_mark_gone_and_reappear(repo: Repo, clock: Clock) -> None:
    listing = repo.upsert_offer(offer("1", 1000, available=True), clock()).listing
    repo.mark_gone(listing.id, clock())
    gone = repo.get_listing(listing.id)
    assert gone is not None and gone.status == "gone" and gone.available is False
    back = repo.upsert_offer(offer("1", 1000, available=True), clock()).listing
    assert back.status == "active"


def test_search_hits_and_due(repo: Repo, clock: Clock) -> None:
    search = repo.create_search(
        clock(), name="n", query="q", sources=["s"], interval_minutes=10, exclude_words=["a"]
    )
    assert search.exclude_words == ["a"] and search.sources == ["s"]
    assert [s.id for s in repo.due_searches(clock())] == [search.id]
    listing = repo.upsert_offer(offer("1", 1000), clock()).listing
    assert repo.record_hit(search.id, listing.id, 1000, clock()) == (True, None)
    assert repo.record_hit(search.id, listing.id, 900, clock()) == (False, 1000)
    clock.advance(minutes=1)
    repo.update_search(search.id, next_run_at=clock.now.replace(hour=23))
    assert repo.due_searches(clock()) == []


def test_migration_is_idempotent(repo: Repo) -> None:
    repo.db.migrate()
    assert repo.db.one("PRAGMA user_version")[0] == 1  # type: ignore[index]
