"""Filtering, de-duplication and sorting of offers from different sources."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from sniflot.sources.base import Condition, Offer

_WORD = re.compile(r"[0-9a-zа-яё]+", re.I)


def norm_title(title: str) -> str:
    """Lowercased word tokens, ``ё`` folded — used for matching and de-duplication."""
    return " ".join(_WORD.findall(title.lower().replace("ё", "е")))


def parse_words(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    items = re.split(r"[,\n]", value) if isinstance(value, str) else list(value)
    return [w for w in (norm_title(i) for i in items) if w]


def has_excluded(title: str, exclude: Sequence[str]) -> bool:
    if not exclude:
        return False
    text = f" {norm_title(title)} "
    return any(f" {word} " in text for word in exclude)


def keep(
    offer: Offer,
    *,
    min_price: int | None = None,
    max_price: int | None = None,
    exclude: Sequence[str] = (),
    condition: Condition = Condition.ANY,
) -> bool:
    """Client-side filter applied to every source (prices in roubles)."""
    if has_excluded(offer.title, exclude):
        return False
    if offer.price is not None:
        if min_price is not None and offer.price < min_price * 100:
            return False
        if max_price is not None and offer.price > max_price * 100:
            return False
    return not (
        condition != Condition.ANY
        and offer.condition != Condition.ANY
        and offer.condition != condition
    )


def dedupe(offers: Iterable[Offer]) -> list[Offer]:
    """Drop repeats of the same listing (same source and id); first one wins."""
    seen: set[tuple[str, str]] = set()
    out: list[Offer] = []
    for offer in offers:
        key = (offer.source, offer.external_id)
        if key not in seen:
            seen.add(key)
            out.append(offer)
    return out
