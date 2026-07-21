"""Compact, human-readable numeric formatting for Telegram views."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def compact_decimal(value: object, *, maximum_places: int | None = None) -> str:
    """Render a decimal without scientific notation or insignificant trailing zeroes."""
    if maximum_places is not None and maximum_places < 0:
        raise ValueError("Telegram decimal maximum places cannot be negative")
    number = _decimal(value)
    rendered = format(number, "f") if maximum_places is None else f"{number:.{maximum_places}f}"
    compact = rendered.rstrip("0").rstrip(".")
    return "0" if compact in {"", "+0", "-0"} else compact


def compact_money(value: object) -> str:
    """Render money to at most four decimal places for a compact dashboard."""
    return compact_decimal(value, maximum_places=4)


def signed_money(value: object) -> str:
    """Render signed money to at most four decimal places."""
    return _signed(value, maximum_places=4)


def signed_percent(value: object) -> str:
    """Render a signed percentage to at most four decimal places."""
    return f"{_signed(value, maximum_places=4)}%"


def _signed(value: object, *, maximum_places: int) -> str:
    number = _decimal(value)
    magnitude = compact_decimal(abs(number), maximum_places=maximum_places)
    if magnitude == "0":
        return "+0"
    sign = "+" if number >= 0 else "-"
    return f"{sign}{magnitude}"


def _decimal(value: object) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Telegram numeric value is invalid") from error
    if not number.is_finite():
        raise ValueError("Telegram numeric value must be finite")
    return number
