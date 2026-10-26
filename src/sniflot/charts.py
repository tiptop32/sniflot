"""Server-side SVG price charts: no JavaScript, no chart library."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from html import escape

from sniflot.repo import PricePoint


def _fmt(kopecks: float) -> str:
    return f"{kopecks / 100:,.0f}".replace(",", " ") + " ₽"


def price_chart(
    points: Sequence[PricePoint],
    now: datetime,
    *,
    width: int = 640,
    height: int = 220,
    label: str = "История цены",
) -> str:
    """Step chart: the price holds until the next recorded change."""
    known = [p for p in points if p.price is not None]
    if not known:
        return '<p class="muted">Истории цены пока нет.</p>'
    pad_l, pad_r, pad_t, pad_b = 72, 12, 14, 28
    start = known[0].at
    end = max(now, known[-1].at)
    span = max((end - start).total_seconds(), 1.0)
    prices = [p.price for p in known if p.price is not None]
    low, high = float(min(prices)), float(max(prices))
    if high - low < 100:  # flat history: give the line some room
        low, high = low * 0.95, high * 1.05 + 100
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b

    def x(at: datetime) -> float:
        return pad_l + (at - start).total_seconds() / span * plot_w

    def y(price: float) -> float:
        return pad_t + (high - price) / (high - low) * plot_h

    path: list[str] = []
    dots: list[str] = []
    for i, point in enumerate(known):
        assert point.price is not None
        px, py = x(point.at), y(point.price)
        if i == 0:
            path.append(f"M{px:.1f},{py:.1f}")
        else:
            path.append(f"H{px:.1f}V{py:.1f}")
        title = f"{point.at:%d.%m.%Y %H:%M} — {_fmt(point.price)}"
        if point.available is False:
            title += " (нет в наличии)"
        cls = "dot off" if point.available is False else "dot"
        dots.append(
            f'<circle class="{cls}" cx="{px:.1f}" cy="{py:.1f}" r="3.5">'
            f"<title>{escape(title)}</title></circle>"
        )
    path.append(f"H{x(end):.1f}")

    grid = []
    for frac in (0.0, 0.5, 1.0):
        value = high - (high - low) * frac
        gy = pad_t + plot_h * frac
        grid.append(
            f'<line class="grid" x1="{pad_l}" x2="{width - pad_r}" y1="{gy:.1f}" y2="{gy:.1f}"/>'
            f'<text class="axis" x="{pad_l - 6}" y="{gy + 4:.1f}" text-anchor="end">'
            f"{_fmt(value)}</text>"
        )
    axis = (
        f'<text class="axis" x="{pad_l}" y="{height - 8}">{start:%d.%m.%y}</text>'
        f'<text class="axis" x="{width - pad_r}" y="{height - 8}" text-anchor="end">'
        f"{end:%d.%m.%y}</text>"
    )
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(label)}">{"".join(grid)}{axis}'
        f'<path class="line" d="{"".join(path)}"/>{"".join(dots)}</svg>'
    )


def sparkline(points: Sequence[PricePoint], *, width: int = 120, height: int = 32) -> str:
    prices = [p.price for p in points if p.price is not None]
    if len(prices) < 2:
        return ""
    low, high = min(prices), max(prices)
    rng = (high - low) or 1
    step = width / (len(prices) - 1)
    coords = " ".join(
        f"{i * step:.1f},{height - 3 - (p - low) / rng * (height - 6):.1f}"
        for i, p in enumerate(prices)
    )
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" aria-hidden="true">'
        f'<polyline points="{coords}"/></svg>'
    )
