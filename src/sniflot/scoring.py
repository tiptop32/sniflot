"""Plain statistics for "is this a good deal?" — no ML, no LLM."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

MIN_PEERS = 5
SUSPICIOUS_RATIO = 0.4  # below 40 % of the peer median looks like fraud or a wrong category
SUSPICIOUS_DROP = 0.5  # a single drop of more than half is worth a second look


@dataclass(frozen=True)
class PeerScore:
    score: float | None  # (median - price) / median; > 0 means cheaper than peers
    suspicious: bool


def peer_scores(prices: Sequence[int | None]) -> list[PeerScore]:
    known = [p for p in prices if p]
    if len(known) < MIN_PEERS:
        return [PeerScore(None, False) for _ in prices]
    mid = median(known)
    out = []
    for price in prices:
        if not price:
            out.append(PeerScore(None, False))
            continue
        out.append(PeerScore(round((mid - price) / mid, 4), price < mid * SUSPICIOUS_RATIO))
    return out


def history_score(current: int | None, history: Sequence[int | None]) -> float | None:
    """How far below its own historical median the current price is."""
    known = [p for p in history if p]
    if not current or len(known) < 2:
        return None
    mid = median(known)
    return round((mid - current) / mid, 4)


def drop_ratio(old: int | None, new: int | None) -> float | None:
    if not old or not new or new >= old:
        return None
    return (old - new) / old


def suspicious_drop(old: int | None, new: int | None) -> bool:
    ratio = drop_ratio(old, new)
    return ratio is not None and ratio > SUSPICIOUS_DROP
