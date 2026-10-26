"""Adapter contract shared by all marketplaces.

Adapters only fetch and parse. They know nothing about the database, schedule
or notifications; ``services`` glues those together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol


class Condition(StrEnum):
    ANY = "any"
    NEW = "new"
    USED = "used"


class Sort(StrEnum):
    RELEVANCE = "relevance"
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"
    NEWEST = "newest"


class SourceError(Exception):
    """Network failure, 5xx or a response we cannot parse. Worth retrying later."""


class BlockedError(SourceError):
    """The site refused access (403/429, CAPTCHA, firewall page).

    Never retried in a loop and never treated as "no results": the source is
    paused instead.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NotFoundError(SourceError):
    """The listing does not exist any more (removed, sold, 404)."""


@dataclass(frozen=True)
class SourceInfo:
    id: str
    name: str
    kind: str  # "shop" (new goods) or "classifieds" (unique used items)
    notes: str = ""
    experimental: bool = False
    # First pause after a block, seconds; doubles on each consecutive block.
    block_pause: float = 1800.0


@dataclass(frozen=True)
class SearchParams:
    query: str
    min_price: int | None = None  # roubles
    max_price: int | None = None  # roubles
    region: str | None = None
    condition: Condition = Condition.ANY
    sort: Sort = Sort.RELEVANCE
    pages: int = 1


@dataclass
class Offer:
    source: str
    external_id: str
    url: str
    title: str
    price: int | None  # kopecks
    old_price: int | None = None
    currency: str = "RUB"
    available: bool | None = None
    condition: Condition = Condition.ANY
    seller: str | None = None
    location: str | None = None
    image_url: str | None = None
    brand: str | None = None
    rating: float | None = None
    reviews: int | None = None
    published_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResult:
    offers: list[Offer]
    source_url: str
    total: int | None = None
    warnings: list[str] = field(default_factory=list)


class Source(Protocol):
    info: SourceInfo

    async def search(self, params: SearchParams) -> SearchResult: ...

    async def fetch(self, ref: str) -> Offer:
        """Fetch one listing by URL or external id."""
        ...

    def parse_ref(self, url: str) -> str | None:
        """Return the external id if ``url`` belongs to this source."""
        ...

    async def aclose(self) -> None: ...
