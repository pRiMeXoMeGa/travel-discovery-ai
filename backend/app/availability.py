"""Deterministic calendar — availability/price computed, never stored.

This is the backend's authoritative copy of the ingestion/availability.py logic.
Both files MUST stay byte-for-byte identical in the _seed / availability /
is_available_range functions so that the availability results are consistent
across services (same listing_id + date always yields the same available/price).

DO NOT modify this file independently of ingestion/availability.py.
"""
import hashlib
from datetime import date, timedelta


def _seed(listing_id: str, day: date) -> int:
    h = hashlib.sha256(f"{listing_id}:{day.isoformat()}".encode()).hexdigest()
    return int(h[:12], 16)


def availability(listing_id: str, day: date, base_price: float) -> dict:
    s = _seed(listing_id, day)
    available = (s % 100) >= 20          # ~80% of nights available
    factor = 0.85 + (s % 31) / 100.0     # nightly price varies 0.85x–1.15x
    return {"available": available, "price": round(base_price * factor, 2)}


def is_available_range(
    listing_id: str,
    check_in: date,
    check_out: date,
    base_price: float,
) -> tuple[bool, float]:
    """Return (all_nights_available, total_price) for [check_in, check_out)."""
    total = 0.0
    day = check_in
    while day < check_out:
        slot = availability(listing_id, day, base_price)
        if not slot["available"]:
            return False, 0.0
        total += slot["price"]
        day += timedelta(days=1)
    return True, round(total, 2)


def availability_window(
    listing_id: str,
    start: date,
    days: int,
    base_price: float,
) -> list[dict]:
    """Return a list of daily availability dicts for `days` nights from `start`.

    Used by the property detail endpoint to render a calendar preview.
    Each dict: {"date": "YYYY-MM-DD", "available": bool, "price": float}
    """
    result = []
    day = start
    for _ in range(days):
        slot = availability(listing_id, day, base_price)
        result.append(
            {
                "date": day.isoformat(),
                "available": slot["available"],
                "price": slot["price"],
            }
        )
        day += timedelta(days=1)
    return result
