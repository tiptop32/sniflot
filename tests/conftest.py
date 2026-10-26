from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from sniflot.config import Settings
from sniflot.db import Database
from sniflot.repo import Repo
from sniflot.services import Service
from sniflot.sources.base import (
    NotFoundError,
    Offer,
    SearchParams,
    SearchResult,
    SourceInfo,
)
from sniflot.sources.http import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class FakeNotifier:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class FakeSource:
    """In-memory source: tests set ``offers``/``items``/``error`` directly."""

    def __init__(self, sid: str = "fake", kind: str = "shop") -> None:
        self.info = SourceInfo(id=sid, name=sid.title(), kind=kind, block_pause=600)
        self.offers: list[Offer] = []
        self.items: dict[str, Offer] = {}
        self.error: Exception | None = None
        self.calls = 0

    def offer(self, ext: str, price_rub: int | None, title: str = "", **kw: Any) -> Offer:
        return Offer(
            source=self.info.id,
            external_id=ext,
            url=f"https://{self.info.id}.example/item/{ext}",
            title=title or f"Товар {ext}",
            price=None if price_rub is None else price_rub * 100,
            **kw,
        )

    async def search(self, params: SearchParams) -> SearchResult:
        self.calls += 1
        if self.error:
            raise self.error
        return SearchResult(list(self.offers), f"https://{self.info.id}.example/s?q={params.query}")

    async def fetch(self, ref: str) -> Offer:
        self.calls += 1
        if self.error:
            raise self.error
        ext = ref.rsplit("/", 1)[-1]
        if ext not in self.items:
            raise NotFoundError(ext)
        return self.items[ext]

    def parse_ref(self, url: str) -> str | None:
        prefix = f"https://{self.info.id}.example/item/"
        return url[len(prefix) :] if url.startswith(prefix) else None

    async def aclose(self) -> None:
        pass


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(Database(tmp_path / "test.db"))


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


@pytest.fixture
def service(repo: Repo, source: FakeSource, notifier: FakeNotifier, clock: Clock) -> Service:
    import random

    settings = Settings(db_path=Path(":memory:"), scheduler_enabled=False)
    return Service(
        settings, repo, {source.info.id: source}, notifier, clock=clock, rng=random.Random(1)
    )


Handler = Callable[[httpx.Request], httpx.Response]


def mock_http(handler: Handler, **kw: Any) -> HttpClient:
    async def no_sleep(_: float) -> None:
        return None

    kw.setdefault("min_interval", 0)
    return HttpClient(
        headers={"User-Agent": "test"},
        transport=httpx.MockTransport(handler),
        sleep=no_sleep,
        **kw,
    )
