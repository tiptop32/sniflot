"""Typed access to the SQLite schema. No network, no business rules."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sniflot.db import Database
from sniflot.normalize import norm_title
from sniflot.sources.base import Offer


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


@dataclass
class Listing:
    id: int
    source: str
    external_id: str
    url: str
    title: str
    price: int | None
    old_price: int | None
    currency: str
    available: bool | None
    condition: str
    seller: str | None
    location: str | None
    image_url: str | None
    brand: str | None
    rating: float | None
    reviews: int | None
    published_at: datetime | None
    status: str
    favorite: bool
    first_seen_at: datetime
    last_seen_at: datetime
    last_checked_at: datetime | None
    norm_title: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Listing:
        return cls(
            id=row["id"],
            source=row["source"],
            external_id=row["external_id"],
            url=row["url"],
            title=row["title"],
            price=row["price"],
            old_price=row["old_price"],
            currency=row["currency"],
            available=_bool(row["available"]),
            condition=row["condition"],
            seller=row["seller"],
            location=row["location"],
            image_url=row["image_url"],
            brand=row["brand"],
            rating=row["rating"],
            reviews=row["reviews"],
            published_at=parse_iso(row["published_at"]),
            status=row["status"],
            favorite=bool(row["favorite"]),
            first_seen_at=parse_iso(row["first_seen_at"]) or utcnow(),
            last_seen_at=parse_iso(row["last_seen_at"]) or utcnow(),
            last_checked_at=parse_iso(row["last_checked_at"]),
            norm_title=row["norm_title"],
            raw=json.loads(row["raw"] or "{}"),
        )


@dataclass
class PricePoint:
    at: datetime
    price: int | None
    available: bool | None


@dataclass
class Upsert:
    listing: Listing
    created: bool
    old_price: int | None
    old_available: bool | None

    @property
    def price_changed(self) -> bool:
        return not self.created and self.old_price != self.listing.price


@dataclass
class Search:
    id: int
    name: str
    query: str
    sources: list[str]
    min_price: int | None
    max_price: int | None
    region: str | None
    condition: str
    sort: str
    exclude_words: list[str]
    pages: int
    interval_minutes: int
    enabled: bool
    notify_new: bool
    notify_drop_pct: float
    baseline_done: bool
    next_run_at: datetime
    last_run_at: datetime | None
    last_status: str | None
    last_error: str | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Search:
        return cls(
            id=row["id"],
            name=row["name"],
            query=row["query"],
            sources=json.loads(row["sources"]),
            min_price=row["min_price"],
            max_price=row["max_price"],
            region=row["region"],
            condition=row["condition"],
            sort=row["sort"],
            exclude_words=json.loads(row["exclude_words"]),
            pages=row["pages"],
            interval_minutes=row["interval_minutes"],
            enabled=bool(row["enabled"]),
            notify_new=bool(row["notify_new"]),
            notify_drop_pct=row["notify_drop_pct"],
            baseline_done=bool(row["baseline_done"]),
            next_run_at=parse_iso(row["next_run_at"]) or utcnow(),
            last_run_at=parse_iso(row["last_run_at"]),
            last_status=row["last_status"],
            last_error=row["last_error"],
            created_at=parse_iso(row["created_at"]) or utcnow(),
        )


@dataclass
class Watch:
    id: int
    listing_id: int
    target_price: int | None
    drop_pct: float
    last_price: int | None
    last_available: bool | None
    interval_minutes: int
    enabled: bool
    next_run_at: datetime
    last_run_at: datetime | None
    last_status: str | None
    last_error: str | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Watch:
        return cls(
            id=row["id"],
            listing_id=row["listing_id"],
            target_price=row["target_price"],
            drop_pct=row["drop_pct"],
            last_price=row["last_price"],
            last_available=_bool(row["last_available"]),
            interval_minutes=row["interval_minutes"],
            enabled=bool(row["enabled"]),
            next_run_at=parse_iso(row["next_run_at"]) or utcnow(),
            last_run_at=parse_iso(row["last_run_at"]),
            last_status=row["last_status"],
            last_error=row["last_error"],
            created_at=parse_iso(row["created_at"]) or utcnow(),
        )


@dataclass
class SourceState:
    source: str
    paused_until: datetime | None = None
    failures: int = 0
    last_ok_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None

    def paused(self, now: datetime) -> bool:
        return self.paused_until is not None and self.paused_until > now


@dataclass
class Event:
    id: int
    at: datetime
    kind: str
    listing_id: int | None
    search_id: int | None
    text: str
    delivered: bool
    error: str | None


_SEARCH_FIELDS = {
    "name",
    "query",
    "sources",
    "min_price",
    "max_price",
    "region",
    "condition",
    "sort",
    "exclude_words",
    "pages",
    "interval_minutes",
    "enabled",
    "notify_new",
    "notify_drop_pct",
    "baseline_done",
    "next_run_at",
    "last_run_at",
    "last_status",
    "last_error",
}
_WATCH_FIELDS = {
    "target_price",
    "drop_pct",
    "last_price",
    "last_available",
    "interval_minutes",
    "enabled",
    "next_run_at",
    "last_run_at",
    "last_status",
    "last_error",
}


def _encode(key: str, value: Any) -> Any:
    if key in ("sources", "exclude_words"):
        return json.dumps(list(value), ensure_ascii=False)
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, bool):
        return int(value)
    return value


class Repo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- listings -----------------------------------------------------------

    def upsert_offer(self, offer: Offer, now: datetime) -> Upsert:
        values = {
            "url": offer.url,
            "title": offer.title,
            "norm_title": norm_title(offer.title),
            "price": offer.price,
            "old_price": offer.old_price,
            "currency": offer.currency,
            "available": None if offer.available is None else int(offer.available),
            "condition": str(offer.condition),
            "seller": offer.seller,
            "location": offer.location,
            "image_url": offer.image_url,
            "brand": offer.brand,
            "rating": offer.rating,
            "reviews": offer.reviews,
            "published_at": iso(offer.published_at),
            "raw": json.dumps(offer.raw, ensure_ascii=False, default=str),
            "now": iso(now),
        }
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM listings WHERE source = ? AND external_id = ?",
                (offer.source, offer.external_id),
            ).fetchone()
            if row is None:
                listing_id = conn.execute(
                    """INSERT INTO listings (source, external_id, url, title, norm_title, price,
                       old_price, currency, available, condition, seller, location, image_url,
                       brand, rating, reviews, published_at, raw, first_seen_at, last_seen_at,
                       last_checked_at)
                       VALUES (:source, :external_id, :url, :title, :norm_title, :price,
                       :old_price, :currency, :available, :condition, :seller, :location,
                       :image_url, :brand, :rating, :reviews, :published_at, :raw, :now, :now,
                       :now)""",
                    {**values, "source": offer.source, "external_id": offer.external_id},
                ).lastrowid
                self._add_point(conn, int(listing_id or 0), offer, now)
                created, old_price, old_available = True, None, None
            else:
                listing_id = row["id"]
                old_price, old_available = row["price"], _bool(row["available"])
                # Keep earlier optional facts when a lighter response omits them.
                conn.execute(
                    """UPDATE listings SET url = :url, title = :title, norm_title = :norm_title,
                       price = :price, old_price = :old_price, currency = :currency,
                       available = COALESCE(:available, available), condition = :condition,
                       seller = COALESCE(:seller, seller), location = COALESCE(:location, location),
                       image_url = COALESCE(:image_url, image_url), brand = COALESCE(:brand, brand),
                       rating = COALESCE(:rating, rating), reviews = COALESCE(:reviews, reviews),
                       published_at = COALESCE(:published_at, published_at), raw = :raw,
                       status = 'active', last_seen_at = :now, last_checked_at = :now
                       WHERE id = :id""",
                    {**values, "id": listing_id},
                )
                new_available = old_available if offer.available is None else offer.available
                if old_price != offer.price or old_available != new_available:
                    self._add_point(conn, listing_id, offer, now)
                created = False
            fresh = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        return Upsert(Listing.from_row(fresh), created, old_price, old_available)

    @staticmethod
    def _add_point(conn: sqlite3.Connection, listing_id: int, offer: Offer, now: datetime) -> None:
        conn.execute(
            "INSERT INTO price_points (listing_id, at, price, available) VALUES (?, ?, ?, ?)",
            (
                listing_id,
                iso(now),
                offer.price,
                None if offer.available is None else int(offer.available),
            ),
        )

    def get_listing(self, listing_id: int) -> Listing | None:
        row = self.db.one("SELECT * FROM listings WHERE id = ?", (listing_id,))
        return Listing.from_row(row) if row else None

    def find_listing(self, source: str, external_id: str) -> Listing | None:
        row = self.db.one(
            "SELECT * FROM listings WHERE source = ? AND external_id = ?", (source, external_id)
        )
        return Listing.from_row(row) if row else None

    def mark_gone(self, listing_id: int, now: datetime) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE listings SET status = 'gone', available = 0, last_checked_at = ? "
                "WHERE id = ?",
                (iso(now), listing_id),
            )
            conn.execute(
                "INSERT INTO price_points (listing_id, at, price, available) "
                "SELECT id, ?, price, 0 FROM listings WHERE id = ?",
                (iso(now), listing_id),
            )

    def set_favorite(self, listing_id: int, favorite: bool) -> None:
        self.db.execute(
            "UPDATE listings SET favorite = ? WHERE id = ?", (int(favorite), listing_id)
        )

    def favorites(self) -> list[Listing]:
        rows = self.db.query("SELECT * FROM listings WHERE favorite = 1 ORDER BY last_seen_at DESC")
        return [Listing.from_row(r) for r in rows]

    def history(self, listing_id: int) -> list[PricePoint]:
        rows = self.db.query(
            "SELECT at, price, available FROM price_points WHERE listing_id = ? ORDER BY at, id",
            (listing_id,),
        )
        return [
            PricePoint(parse_iso(r["at"]) or utcnow(), r["price"], _bool(r["available"]))
            for r in rows
        ]

    def histories(self, listing_ids: list[int]) -> dict[int, list[int | None]]:
        if not listing_ids:
            return {}
        marks = ",".join("?" * len(listing_ids))
        rows = self.db.query(
            f"SELECT listing_id, price FROM price_points WHERE listing_id IN ({marks}) "
            "ORDER BY at, id",
            tuple(listing_ids),
        )
        out: dict[int, list[int | None]] = {i: [] for i in listing_ids}
        for row in rows:
            out[row["listing_id"]].append(row["price"])
        return out

    # --- searches -----------------------------------------------------------

    def create_search(self, now: datetime, **fields: Any) -> Search:
        fields = {k: _encode(k, v) for k, v in fields.items() if k in _SEARCH_FIELDS}
        fields.setdefault("next_run_at", iso(now))
        fields["created_at"] = iso(now)
        columns = ", ".join(fields)
        marks = ", ".join(f":{k}" for k in fields)
        search_id = self.db.execute(f"INSERT INTO searches ({columns}) VALUES ({marks})", fields)
        found = self.get_search(search_id)
        assert found is not None
        return found

    def update_search(self, search_id: int, **fields: Any) -> None:
        fields = {k: _encode(k, v) for k, v in fields.items() if k in _SEARCH_FIELDS}
        if fields:
            sets = ", ".join(f"{k} = :{k}" for k in fields)
            self.db.execute(
                f"UPDATE searches SET {sets} WHERE id = :id", {**fields, "id": search_id}
            )

    def delete_search(self, search_id: int) -> None:
        self.db.execute("DELETE FROM searches WHERE id = ?", (search_id,))

    def get_search(self, search_id: int) -> Search | None:
        row = self.db.one("SELECT * FROM searches WHERE id = ?", (search_id,))
        return Search.from_row(row) if row else None

    def searches(self) -> list[Search]:
        return [Search.from_row(r) for r in self.db.query("SELECT * FROM searches ORDER BY id")]

    def due_searches(self, now: datetime) -> list[Search]:
        rows = self.db.query(
            "SELECT * FROM searches WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at",
            (iso(now),),
        )
        return [Search.from_row(r) for r in rows]

    def record_hit(
        self, search_id: int, listing_id: int, price: int | None, now: datetime
    ) -> tuple[bool, int | None]:
        """Remember that a search returned a listing. Returns (first time, previous price)."""
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT last_price FROM search_hits WHERE search_id = ? AND listing_id = ?",
                (search_id, listing_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO search_hits (search_id, listing_id, first_seen_at, last_seen_at, "
                    "last_price) VALUES (?, ?, ?, ?, ?)",
                    (search_id, listing_id, iso(now), iso(now), price),
                )
                return True, None
            conn.execute(
                "UPDATE search_hits SET last_seen_at = ?, last_price = ? "
                "WHERE search_id = ? AND listing_id = ?",
                (iso(now), price, search_id, listing_id),
            )
            return False, row["last_price"]

    def search_listings(self, search_id: int, limit: int = 500) -> list[tuple[Listing, datetime]]:
        rows = self.db.query(
            "SELECT l.*, h.first_seen_at AS hit_first_seen, h.last_seen_at AS hit_last_seen "
            "FROM search_hits h JOIN listings l ON l.id = h.listing_id "
            "WHERE h.search_id = ? ORDER BY h.last_seen_at DESC, h.first_seen_at DESC LIMIT ?",
            (search_id, limit),
        )
        return [(Listing.from_row(r), parse_iso(r["hit_last_seen"]) or utcnow()) for r in rows]

    # --- watches ------------------------------------------------------------

    def create_watch(self, listing_id: int, now: datetime, **fields: Any) -> Watch:
        fields = {k: _encode(k, v) for k, v in fields.items() if k in _WATCH_FIELDS}
        fields.setdefault("next_run_at", iso(now))
        fields.update(listing_id=listing_id, created_at=iso(now))
        columns = ", ".join(fields)
        marks = ", ".join(f":{k}" for k in fields)
        self.db.execute(
            f"INSERT INTO watches ({columns}) VALUES ({marks}) ON CONFLICT(listing_id) DO NOTHING",
            fields,
        )
        found = self.watch_for_listing(listing_id)
        assert found is not None
        return found

    def update_watch(self, watch_id: int, **fields: Any) -> None:
        fields = {k: _encode(k, v) for k, v in fields.items() if k in _WATCH_FIELDS}
        if fields:
            sets = ", ".join(f"{k} = :{k}" for k in fields)
            self.db.execute(f"UPDATE watches SET {sets} WHERE id = :id", {**fields, "id": watch_id})

    def delete_watch(self, watch_id: int) -> None:
        self.db.execute("DELETE FROM watches WHERE id = ?", (watch_id,))

    def get_watch(self, watch_id: int) -> Watch | None:
        row = self.db.one("SELECT * FROM watches WHERE id = ?", (watch_id,))
        return Watch.from_row(row) if row else None

    def watch_for_listing(self, listing_id: int) -> Watch | None:
        row = self.db.one("SELECT * FROM watches WHERE listing_id = ?", (listing_id,))
        return Watch.from_row(row) if row else None

    def watches(self) -> list[tuple[Watch, Listing]]:
        rows = self.db.query(
            "SELECT w.*, l.id AS l_id FROM watches w JOIN listings l ON l.id = w.listing_id "
            "ORDER BY w.id"
        )
        out = []
        for row in rows:
            listing = self.get_listing(row["l_id"])
            if listing:
                out.append((Watch.from_row(row), listing))
        return out

    def due_watches(self, now: datetime) -> list[Watch]:
        rows = self.db.query(
            "SELECT * FROM watches WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at",
            (iso(now),),
        )
        return [Watch.from_row(r) for r in rows]

    # --- source state -------------------------------------------------------

    def source_state(self, source: str) -> SourceState:
        row = self.db.one("SELECT * FROM source_state WHERE source = ?", (source,))
        if row is None:
            return SourceState(source)
        return SourceState(
            source=source,
            paused_until=parse_iso(row["paused_until"]),
            failures=row["failures"],
            last_ok_at=parse_iso(row["last_ok_at"]),
            last_error=row["last_error"],
            last_error_at=parse_iso(row["last_error_at"]),
        )

    def save_source_state(self, state: SourceState) -> None:
        self.db.execute(
            """INSERT INTO source_state (source, paused_until, failures, last_ok_at, last_error,
               last_error_at) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET paused_until = excluded.paused_until,
               failures = excluded.failures, last_ok_at = excluded.last_ok_at,
               last_error = excluded.last_error, last_error_at = excluded.last_error_at""",
            (
                state.source,
                iso(state.paused_until),
                state.failures,
                iso(state.last_ok_at),
                state.last_error,
                iso(state.last_error_at),
            ),
        )

    # --- events -------------------------------------------------------------

    def add_event(
        self,
        kind: str,
        text: str,
        now: datetime,
        listing_id: int | None = None,
        search_id: int | None = None,
    ) -> int:
        return self.db.execute(
            "INSERT INTO events (at, kind, listing_id, search_id, text) VALUES (?, ?, ?, ?, ?)",
            (iso(now), kind, listing_id, search_id, text),
        )

    def mark_event(self, event_id: int, delivered: bool, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE events SET delivered = ?, error = ? WHERE id = ?",
            (int(delivered), error, event_id),
        )

    def events(self, limit: int = 50) -> list[Event]:
        rows = self.db.query("SELECT * FROM events ORDER BY at DESC, id DESC LIMIT ?", (limit,))
        return [
            Event(
                id=r["id"],
                at=parse_iso(r["at"]) or utcnow(),
                kind=r["kind"],
                listing_id=r["listing_id"],
                search_id=r["search_id"],
                text=r["text"],
                delivered=bool(r["delivered"]),
                error=r["error"],
            )
            for r in rows
        ]
