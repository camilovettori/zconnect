from decimal import Decimal, ROUND_HALF_UP
from typing import Union


Number = Union[int, float, str, Decimal]


def normalize_unify_money(value: Number) -> float:
    amount = Decimal(str(value)) / Decimal("100")
    return float(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def unify_minor_to_major(value: Number) -> float:
    return normalize_unify_money(value)


def major_to_minor(value: Number) -> int:
    amount = Decimal(str(value)) * Decimal("100")
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_currency_eur(value: Number) -> str:
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"€{amount:,.2f}"
