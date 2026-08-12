"""Availability service: wraps `app/availability.py`'s deterministic
calendar with a date-range -> per-night helper.

`is_available_range()` (app/availability.py) returns only `(bool, total)`
for a `[check_in, check_out)` range — no per-night breakdown. Per-night rows
only come from `availability_window(listing_id, start, days, base_price)`,
which takes a night COUNT, not a date range. WS2's `check_availability` MCP
tool needs both the aggregate answer and the per-night rows from one call,
so this module does the range -> days conversion once here instead of every
caller reimplementing `(check_out - check_in).days`.
"""
from datetime import date

from ..availability import availability_window, is_available_range

__all__ = ["availability_window", "is_available_range", "check_availability_range"]


def check_availability_range(
    listing_id: str, check_in: date, check_out: date, base_price: float
) -> dict:
    """Return per-night rows plus an aggregate for `[check_in, check_out)`.

    Shape: {"available": bool, "total_price": float, "nights": [
    {"date": iso, "available": bool, "price": float}, ... ]}.

    `available` is True only when every night in the window is available —
    matching `is_available_range`'s semantics exactly. `total_price` sums
    the per-night prices only in that case (0.0 otherwise, the same
    convention `is_available_range` uses), so the two functions can never
    disagree for the same inputs — both are folds over the same
    deterministic per-night `availability()` calls.

    A non-positive range (`check_out <= check_in`) returns an empty,
    unavailable result rather than raising — callers pass user-controlled
    dates and this is a read path, not a validation boundary.
    """
    days = (check_out - check_in).days
    if days <= 0:
        return {"available": False, "total_price": 0.0, "nights": []}

    nights = availability_window(listing_id, check_in, days, base_price)
    available = all(n["available"] for n in nights)
    total_price = round(sum(n["price"] for n in nights), 2) if available else 0.0
    return {"available": available, "total_price": total_price, "nights": nights}
