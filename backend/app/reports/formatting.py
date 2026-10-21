"""Irish business formatting: euro amounts as "€1,234.56", dates as "13 Oct 2026", times in
Irish time (IST/GMT handled by Europe/Dublin)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional, Union

from .periods import IRELAND

NOT_RECORDED = "Not recorded"
CURRENCY_SYMBOLS = {"EUR": "€", "GBP": "£", "USD": "$"}

Number = Union[Decimal, str, int, float, None]


def to_decimal(value: Number) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def money(value: Number, currency: Optional[str] = "EUR") -> str:
    amount = to_decimal(value)
    if amount is None:
        return NOT_RECORDED
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    symbol = CURRENCY_SYMBOLS.get(currency or "EUR", f"{currency} ")
    sign = "-" if amount < 0 else ""
    return f"{sign}{symbol}{abs(amount):,.2f}"


def quantity(value: Number) -> str:
    amount = to_decimal(value)
    if amount is None:
        return "–"
    amount = amount.normalize()
    return f"{amount:,.0f}" if amount == amount.to_integral() else f"{amount:,.3f}".rstrip("0").rstrip(".")


def count(value: Optional[int]) -> str:
    return NOT_RECORDED if value is None else f"{value:,}"


def percent(part: Number, whole: Number) -> str:
    p, w = to_decimal(part), to_decimal(whole)
    if p is None or not w:
        return "–"
    return f"{(p / w * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def change(current: Number, previous: Number) -> Optional[str]:
    c, p = to_decimal(current), to_decimal(previous)
    if c is None or p is None or p == 0:
        return None
    pct = ((c - p) / p * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if pct == 0:
        return "No change vs previous period"
    return f"{'+' if pct > 0 else ''}{pct}% vs previous period"


def day(value: date) -> str:
    return f"{value.day} {value.strftime('%b %Y')}"


def day_range(start: date, end: date) -> str:
    if start == end:
        return f"{start.strftime('%a')} {day(start)}"
    left = f"{start.strftime('%a')} {start.day} {start.strftime('%b')}" + ("" if start.year == end.year else f" {start.year}")
    return f"{left} – {end.strftime('%a')} {day(end)}"


def generated_at(value: datetime) -> str:
    local = value.astimezone(IRELAND)
    return f"{day(local.date())}, {local.strftime('%H:%M')} Irish time"
