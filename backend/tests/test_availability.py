"""Tests for the deterministic availability calendar (app.availability).

`backend/app/availability.py` is pure (hashlib + datetime, zero I/O), so
these tests need none of the sys.modules stubbing from conftest.py — they
exercise the real module directly.

Also enforces the documented "byte-for-byte identical" invariant between
`backend/app/availability.py` and `ingestion/availability.py`: both files
carry a comment saying they MUST stay in sync (same hash, same params) so
availability results are consistent across services, but nothing currently
checks that automatically. `test_backend_and_ingestion_stay_in_sync` is
that check.
"""
from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.availability import (
    availability,
    availability_window,
    is_available_range,
)


def _load_ingestion_availability():
    """Import ingestion/availability.py by file path.

    `ingestion/` is not an installed package (no __init__.py, and its
    sibling modules like ingest.py pull in heavier deps we don't want to
    trigger) — but availability.py itself has zero external imports, so
    loading it directly by path is safe and doesn't need it on sys.path.
    """
    path = (
        Path(__file__).resolve().parents[2] / "ingestion" / "availability.py"
    )
    spec = importlib.util.spec_from_file_location("ingestion_availability", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ingestion_availability():
    return _load_ingestion_availability()


# ── Determinism ──────────────────────────────────────────────────────────────
def test_availability_is_deterministic():
    listing_id = "listing-abc-123"
    day = date(2026, 6, 15)
    first = availability(listing_id, day, base_price=100.0)
    second = availability(listing_id, day, base_price=100.0)
    assert first == second


def test_availability_deterministic_across_many_inputs():
    base_price = 150.0
    day = date(2026, 1, 1)
    for i in range(50):
        listing_id = f"listing-{i}"
        for offset in range(5):
            d = day + timedelta(days=offset)
            assert availability(listing_id, d, base_price) == availability(
                listing_id, d, base_price
            )


def test_different_listing_or_date_can_differ():
    # Sanity check the seed actually depends on both inputs — otherwise the
    # determinism tests above would be vacuously true.
    base_price = 100.0
    day = date(2026, 3, 1)
    results = {
        (lid, d): availability(lid, day + timedelta(days=d), base_price)["price"]
        for lid in ("listing-a", "listing-b")
        for d in range(10)
    }
    assert len(set(results.values())) > 1


# ── ~80% availability rate ──────────────────────────────────────────────────
def test_availability_rate_is_approximately_80_percent():
    base_price = 100.0
    n = 6000
    day = date(2026, 1, 1)
    available_count = sum(
        1
        for i in range(n)
        if availability(f"listing-{i}", day + timedelta(days=i % 30), base_price)[
            "available"
        ]
    )
    rate = available_count / n
    # s % 100 >= 20 => exactly 80/100 outcomes; SHA256 output is uniform
    # enough that a 6000-sample rate should land tightly around 0.80.
    assert 0.75 <= rate <= 0.85, f"availability rate {rate} outside expected band"


# ── Price factor bounds: 0.85x - 1.15x ───────────────────────────────────────
def test_price_factor_within_bounds():
    base_price = 100.0  # round number avoids rounding-induced edge noise
    day = date(2026, 1, 1)
    for i in range(2000):
        slot = availability(f"listing-{i}", day + timedelta(days=i % 45), base_price)
        factor = slot["price"] / base_price
        assert 0.85 - 1e-6 <= factor <= 1.15 + 1e-6, (
            f"price factor {factor} out of [0.85, 1.15] for listing-{i}"
        )


def test_price_factor_spans_the_range():
    # Not just bounded — should actually get close to both ends given enough
    # samples, otherwise the bounds test above would pass trivially.
    base_price = 100.0
    day = date(2026, 1, 1)
    factors = [
        availability(f"listing-{i}", day, base_price)["price"] / base_price
        for i in range(3000)
    ]
    assert min(factors) <= 0.87
    assert max(factors) >= 1.13


# ── is_available_range ───────────────────────────────────────────────────────
def test_is_available_range_false_on_any_unavailable_night():
    listing_id = "listing-range-scan"
    base_price = 120.0
    start = date(2026, 1, 1)
    # Pure/deterministic function: scanning forward for a window containing
    # at least one unavailable night is not flaky, just a fixed computation.
    for length in range(1, 60):
        check_out = start + timedelta(days=length)
        nights = [
            availability(listing_id, start + timedelta(days=d), base_price)
            for d in range(length)
        ]
        if any(not n["available"] for n in nights):
            ok, total = is_available_range(listing_id, start, check_out, base_price)
            assert ok is False
            assert total == 0.0
            return
    pytest.fail("no unavailable night found scanning 60 days — check hash distribution")


def test_is_available_range_true_sums_prices_when_all_available():
    listing_id = "listing-range-all-available"
    base_price = 90.0
    start = date(2026, 1, 1)
    for length in range(1, 60):
        check_out = start + timedelta(days=length)
        nights = [
            availability(listing_id, start + timedelta(days=d), base_price)
            for d in range(length)
        ]
        if all(n["available"] for n in nights):
            ok, total = is_available_range(listing_id, start, check_out, base_price)
            assert ok is True
            assert total == round(sum(n["price"] for n in nights), 2)
            return
    pytest.fail("no fully-available window found scanning 60 days — check hash distribution")


def test_is_available_range_empty_range_is_available_with_zero_total():
    listing_id = "listing-empty-range"
    day = date(2026, 5, 1)
    ok, total = is_available_range(listing_id, day, day, base_price=100.0)
    assert ok is True
    assert total == 0.0


# ── availability_window ──────────────────────────────────────────────────────
def test_availability_window_length_and_shape():
    listing_id = "listing-window-1"
    start = date(2026, 2, 1)
    days = 14
    base_price = 200.0
    window = availability_window(listing_id, start, days, base_price)

    assert len(window) == days
    for offset, entry in enumerate(window):
        assert set(entry.keys()) == {"date", "available", "price"}
        assert entry["date"] == (start + timedelta(days=offset)).isoformat()
        assert isinstance(entry["available"], bool)
        assert isinstance(entry["price"], float)


def test_availability_window_matches_availability_per_day():
    listing_id = "listing-window-2"
    start = date(2026, 4, 10)
    days = 10
    base_price = 175.0
    window = availability_window(listing_id, start, days, base_price)

    for offset, entry in enumerate(window):
        expected = availability(listing_id, start + timedelta(days=offset), base_price)
        assert entry["available"] == expected["available"]
        assert entry["price"] == expected["price"]


def test_availability_window_zero_days_is_empty():
    assert availability_window("listing-x", date(2026, 1, 1), 0, 100.0) == []


# ── backend/app/availability.py <-> ingestion/availability.py parity ────────
def test_backend_and_ingestion_stay_in_sync(ingestion_availability):
    """The two copies are documented as needing to stay byte-for-byte
    identical (same hash, same params) so availability is consistent across
    services. Nothing enforces that automatically — this does.
    """
    base_price = 133.0
    start = date(2026, 1, 1)
    for i in range(500):
        listing_id = f"sync-listing-{i}"
        day = start + timedelta(days=i % 90)
        backend_result = availability(listing_id, day, base_price)
        ingestion_result = ingestion_availability.availability(
            listing_id, day, base_price
        )
        assert backend_result == ingestion_result, (
            f"backend/ingestion availability diverged for {listing_id}/{day}"
        )


def test_backend_and_ingestion_is_available_range_stay_in_sync(ingestion_availability):
    base_price = 210.0
    listing_id = "sync-range-listing"
    start = date(2026, 3, 1)
    for length in (1, 3, 7, 14, 30):
        check_out = start + timedelta(days=length)
        backend_result = is_available_range(listing_id, start, check_out, base_price)
        ingestion_result = ingestion_availability.is_available_range(
            listing_id, start, check_out, base_price
        )
        assert backend_result == ingestion_result
