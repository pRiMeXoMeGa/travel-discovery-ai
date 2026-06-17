"""Deterministic calendar — availability/price computed, never stored.

Avoids materializing ~50K listings x 365 days = ~18M rows (over the free-tier
cap). Given the same (listing_id, date) this always returns the same result, so
it behaves like a real, stable calendar for the date-range filter. Used by both
ingestion (if it ever needs to seed) and the backend search/detail endpoints.

The backend has its own copy of this logic; keep the two in sync (same hash,
same params) so availability is consistent across services.
"""
import hashlib
from datetime import date


def _seed(listing_id: str, day: date) -> int:
    h = hashlib.sha256(f"{listing_id}:{day.isoformat()}".encode()).hexdigest()
    return int(h[:12], 16)


def availability(listing_id: str, day: date, base_price: float) -> dict:
    s = _seed(listing_id, day)
    available = (s % 100) >= 20          # ~80% of nights available
    factor = 0.85 + (s % 31) / 100.0     # nightly price varies 0.85x–1.15x
    return {"available": available, "price": round(base_price * factor, 2)}


def is_available_range(listing_id: str, check_in: date, check_out: date, base_price: float) -> tuple[bool, float]:
    """Return (all_nights_available, total_price) for [check_in, check_out)."""
    from datetime import timedelta

    total = 0.0
    day = check_in
    while day < check_out:
        slot = availability(listing_id, day, base_price)
        if not slot["available"]:
            return False, 0.0
        total += slot["price"]
        day += timedelta(days=1)
    return True, round(total, 2)
