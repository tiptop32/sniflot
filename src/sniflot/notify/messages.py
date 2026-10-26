"""Human-readable texts for events (Telegram HTML subset)."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from sniflot.repo import Listing


def rub(kopecks: int | None) -> str:
    if kopecks is None:
        return "—"
    roubles = kopecks / 100
    text = f"{roubles:,.0f}" if roubles == int(roubles) else f"{roubles:,.2f}"
    return text.replace(",", " ") + " ₽"


def link(listing: Listing) -> str:
    return f'<a href="{escape(listing.url)}">{escape(listing.title[:120])}</a>'


def new_listings(search_name: str, listings: Sequence[Listing], more: int = 0) -> str:
    lines = [f"🆕 <b>{escape(search_name)}</b>: новых объявлений — {len(listings) + more}"]
    for listing in listings:
        where = f" · {escape(listing.location)}" if listing.location else ""
        lines.append(f"• {rub(listing.price)} — {link(listing)} <i>[{listing.source}]</i>{where}")
    if more:
        lines.append(f"…и ещё {more}")
    return "\n".join(lines)


def price_drop(
    listing: Listing, old: int | None, context: str = "", suspicious: bool = False
) -> str:
    pct = ""
    if old and listing.price:
        pct = f" (−{(old - listing.price) / old * 100:.0f}%)"
    warn = "\n⚠️ Слишком резкое падение — проверьте объявление" if suspicious else ""
    ctx = f" · {escape(context)}" if context else ""
    return f"📉 {rub(old)} → <b>{rub(listing.price)}</b>{pct}{ctx}\n{link(listing)}{warn}"


def target_reached(listing: Listing, target: int) -> str:
    return f"🎯 Цена {rub(listing.price)} ≤ цели {rub(target)}\n{link(listing)}"


def gone(listing: Listing) -> str:
    return f"❌ Объявление снято или удалено\n{link(listing)}"


def out_of_stock(listing: Listing) -> str:
    return f"📦 Нет в наличии\n{link(listing)}"


def back_in_stock(listing: Listing) -> str:
    return f"✅ Снова в наличии: {rub(listing.price)}\n{link(listing)}"


def source_blocked(source: str, error: str, minutes: int) -> str:
    return (
        f"⛔ Источник <b>{escape(source)}</b> ограничил доступ: {escape(error)}\n"
        f"Проверки приостановлены на {minutes} мин."
    )
