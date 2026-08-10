"""Unit tests for the retrieval agent — WS0-H (summaries fusion), WS0-E (area
constraints) and the WS0-G amenity-vocabulary unification.

Relies on `backend/tests/conftest.py` for the heavy-dependency stubs (qdrant,
asyncpg, redis, fastembed) and the no-network guard. Per that file's contract
the qdrant `models.*` stubs are permissive kwargs-storing classes, so the filter
tests assert on the *structure we build* (nested Filter / FieldCondition /
MatchValue attributes) rather than on real Qdrant matching behaviour, and the
fusion tests drive a fake client + fake pool rather than a real vector store.

Async coroutines are driven with `asyncio.run` so the file does not depend on
pytest-asyncio being configured.

Not covered here (impossible without infrastructure): that the fused query
actually returns better listings from a live Qdrant. See the report.
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.agents import retrieval  # noqa: E402
from app.config import Settings  # noqa: E402
from app.schemas import StructuredQuery  # noqa: E402

_REPO_ROOT = _BACKEND.parent


# ── Helpers ──────────────────────────────────────────────────────────────────
def _hit(listing_id: str, score: float):
    """A qdrant ScoredPoint stand-in (only .payload / .score are read)."""
    return types.SimpleNamespace(payload={"listing_id": listing_id}, score=score)


def _row(listing_id: str, **over):
    row = {
        "id": listing_id,
        "name": f"Listing {listing_id}",
        "type": "Entire home/apt",
        "city": "Lisbon",
        "neighbourhood": "Santa Maria Maior",
        "lat": 38.71,
        "lng": -9.13,
        "base_price": 100.0,
        "beds": 2,
        "amenities": ["wifi", "kitchen"],
        "photos": ["http://example/1.jpg"],
        "rating": 4.6,
        "review_count": 12,
    }
    row.update(over)
    return row


def _conditions(qfilter, field):
    """Flatten every FieldCondition on `field` in must + nested should."""
    out = []
    for cond in list(getattr(qfilter, "must", None) or []):
        if getattr(cond, "key", None) == field:
            out.append(cond)
        for nested in list(getattr(cond, "should", None) or []):
            if getattr(nested, "key", None) == field:
                out.append(nested)
    return out


def _values(conds):
    return [c.match.value for c in conds]


@pytest.fixture
def wired(monkeypatch):
    """Wire `retrieval` to fakes and return a mutable control dict."""
    ctl = {
        "listing_hits": [],
        "summary_points": [],
        "summary_exc": None,
        "rows": {},
        "embed_calls": [],
        "query_points_kwargs": [],
        "search_kwargs": [],
    }

    async def fake_embed(text):
        ctl["embed_calls"].append(text)
        return [0.5] * 384

    class FakeClient:
        async def search(self, **kwargs):
            ctl["search_kwargs"].append(kwargs)
            return list(ctl["listing_hits"])

        async def query_points(self, **kwargs):
            ctl["query_points_kwargs"].append(kwargs)
            if ctl["summary_exc"] is not None:
                raise ctl["summary_exc"]
            return types.SimpleNamespace(points=list(ctl["summary_points"]))

    async def fake_hydrate(ids):
        return {i: ctl["rows"][i] for i in ids if i in ctl["rows"]}

    async def fake_cache_get(_key):
        return None

    async def fake_cache_set(*_a, **_k):
        return None

    monkeypatch.setattr(retrieval, "embed_query", fake_embed)
    monkeypatch.setattr(retrieval, "get_qdrant", lambda: FakeClient())
    monkeypatch.setattr(retrieval, "_hydrate", fake_hydrate)
    monkeypatch.setattr(retrieval, "cache_get", fake_cache_get)
    monkeypatch.setattr(retrieval, "cache_set", fake_cache_set)
    monkeypatch.setattr(retrieval, "_summaries_missing", False)
    return ctl


# ═════════════════════════════════════════════════════════════════════════════
# WS0-G — one amenity vocabulary
# ═════════════════════════════════════════════════════════════════════════════
def _load_ingestion_enrich():
    """Import ingestion/enrich.py by path (ingestion/ is not a package)."""
    path = _REPO_ROOT / "ingestion" / "enrich.py"
    spec = importlib.util.spec_from_file_location("_ingestion_enrich_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_amenity_vocabulary_does_not_drift_from_ingestion():
    """The mirrored constant must equal ingestion's authoritative list.

    `retrieval.CANONICAL_AMENITIES` is mirrored, not imported, because the
    backend image copies only `app/`. This test is what keeps the mirror honest.
    """
    enrich = _load_ingestion_enrich()
    assert list(retrieval.CANONICAL_AMENITIES) == list(enrich.CANONICAL_AMENITIES)
    assert retrieval._KNOWN_AMENITIES == frozenset(enrich.CANONICAL_AMENITIES)


def test_phantom_amenities_are_gone_and_baby_cot_survives():
    # dryer / heating / tv are not in the payload vocabulary — they could never
    # match, so they must not be accepted as constraints (FINDINGS §2.10).
    for phantom in ("dryer", "heating", "tv"):
        assert retrieval._normalize_amenity(phantom) is None
    # baby_cot was silently dropped before; it is canonical.
    assert retrieval._normalize_amenity("baby_cot") == "baby_cot"
    assert retrieval._normalize_amenity("baby cot") == "baby_cot"
    assert retrieval._normalize_amenity("crib") == "baby_cot"


def test_key_amenity_priority_is_a_subset_of_the_vocabulary():
    assert set(retrieval._KEY_AMENITY_PRIORITY) <= set(retrieval.CANONICAL_AMENITIES)


def test_baby_cot_reaches_the_qdrant_filter():
    sq = StructuredQuery(city="Lisbon", hard_constraints=["baby cot"])
    parsed = retrieval._parse_constraints(sq)
    assert parsed["amenities"] == ["baby_cot"]
    qf = retrieval._build_qdrant_filter(sq, parsed)
    assert "baby_cot" in _values(_conditions(qf, "amenities"))


def test_config_defaults_align_with_production():
    s = Settings(_env_file=None)
    assert s.gemini_model == "gemini-3.1-flash-lite"
    assert s.qdrant_collection_summaries == "summaries"


# ═════════════════════════════════════════════════════════════════════════════
# _parse_constraints — public contract (routers/agents.py imports it)
# ═════════════════════════════════════════════════════════════════════════════
def test_parse_constraints_contract_is_stable():
    import inspect

    sig = inspect.signature(retrieval._parse_constraints)
    assert list(sig.parameters) == ["sq"]
    parsed = retrieval._parse_constraints(StructuredQuery())
    assert set(parsed) == {"amenities", "avoid_areas", "near_areas", "property_type"}


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("near Downtown LA", "downtown la"),
        ("close to the beach", "the beach"),
        ("next to Griffith Park", "griffith park"),
        ("walking distance to the metro", "the metro"),
    ],
)
def test_near_prefixes_are_extracted(phrase, expected):
    parsed = retrieval._parse_constraints(StructuredQuery(hard_constraints=[phrase]))
    assert parsed["near_areas"] == [expected]


def test_near_prefix_matching_is_anchored():
    """The old mid-string `.replace("near", "")` mangled unrelated words."""
    parsed = retrieval._parse_constraints(
        StructuredQuery(soft_preferences=["the nearest supermarket"])
    )
    assert parsed["near_areas"] == []


# ═════════════════════════════════════════════════════════════════════════════
# WS0-E — area resolution against the REAL corpus neighbourhood values
# ═════════════════════════════════════════════════════════════════════════════
def test_downtown_la_resolves_to_downtown_not_long_beach():
    """EVAL failure #2: "near Downtown LA" drifted to Long Beach."""
    sq = StructuredQuery(city="Los Angeles", hard_constraints=["near Downtown LA"])
    qf = retrieval._build_qdrant_filter(sq, retrieval._parse_constraints(sq))
    values = _values(_conditions(qf, "neighbourhood"))
    assert values == ["Downtown"]
    assert "Long Beach" not in values


def test_near_area_is_a_nested_should_filter_inside_must():
    """One AND term that ORs over the neighbourhood values."""
    sq = StructuredQuery(city="Amsterdam", hard_constraints=["near the centre"])
    qf = retrieval._build_qdrant_filter(sq, retrieval._parse_constraints(sq))
    nested = [c for c in qf.must if getattr(c, "should", None)]
    assert len(nested) == 1
    assert _values(nested[0].should) == ["Centrum-West", "Centrum-Oost"]
    # ...and every leaf targets the already-indexed keyword field.
    assert all(c.key == "neighbourhood" for c in nested[0].should)


def test_longest_alias_wins():
    sq = StructuredQuery(city="Los Angeles", hard_constraints=["near West Hollywood"])
    qf = retrieval._build_qdrant_filter(sq, retrieval._parse_constraints(sq))
    assert _values(_conditions(qf, "neighbourhood")) == ["West Hollywood"]


def test_lisbon_values_are_the_accent_stripped_corpus_forms():
    """csvData/lisbon/listings.csv stores "Misericrdia", not "Misericórdia"."""
    assert retrieval._resolve_place("Lisbon", "chiado") == ("Misericrdia",)
    assert retrieval._resolve_place("Lisbon", "belem") == ("Belm",)
    # ...and the user may well type the properly accented form.
    assert retrieval._resolve_place("Lisbon", "Belém") == ("Belm",)
    assert retrieval._resolve_place("Lisbon", "Alcântara") == ("Alcntara",)


def test_area_resolution_is_city_scoped():
    # "downtown" means different neighbourhoods in each corpus, and nothing at
    # all when the city is unknown.
    assert retrieval._resolve_place("Los Angeles", "downtown") == ("Downtown",)
    assert retrieval._resolve_place("Amsterdam", "downtown") == (
        "Centrum-West",
        "Centrum-Oost",
    )
    assert retrieval._resolve_place(None, "downtown") == ()
    assert retrieval._resolve_place("Berlin", "downtown") == ()


def test_unresolvable_place_adds_no_condition():
    """An unknown landmark must not become a filter that excludes everything."""
    sq = StructuredQuery(city="Lisbon", hard_constraints=["near the moon base"])
    qf = retrieval._build_qdrant_filter(sq, retrieval._parse_constraints(sq))
    assert _conditions(qf, "neighbourhood") == []


def test_avoid_areas_still_produce_must_not_and_alias_expand():
    sq = StructuredQuery(
        city="Amsterdam", hard_constraints=["avoid de pijp", "avoid Osdorp"]
    )
    qf = retrieval._build_qdrant_filter(sq, retrieval._parse_constraints(sq))
    values = [c.match.value for c in qf.must_not]
    # alias-expanded to the real value...
    assert "De Pijp - Rivierenbuurt" in values
    # ...and the pre-WS0-E .title() fallback still covers bare names.
    assert "Osdorp" in values


_CITY_FOLDERS = {
    "Amsterdam": "amsterdam",
    "Lisbon": "lisbon",
    "Los Angeles": "los angeles",
}


@pytest.mark.parametrize("city,folder", sorted(_CITY_FOLDERS.items()))
def test_every_alias_value_exists_in_the_source_corpus(city, folder):
    """No alias may point at a neighbourhood the corpus does not contain.

    A typo here is invisible at runtime — the filter simply matches nothing —
    so it is checked against `neighbourhood_cleansed`, the column
    `ingestion/ingest.py:656-661` copies verbatim into the payload.
    `csvData/` is gitignored, so this skips where the corpus is not present.
    """
    import csv

    path = _REPO_ROOT / "csvData" / folder / "listings.csv"
    if not path.exists():
        pytest.skip(f"{path} not present (csvData is gitignored)")
    csv.field_size_limit(10_000_000)
    real = set()
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            value = (row.get("neighbourhood_cleansed") or "").strip()
            if value:
                real.add(value)

    declared = {v for vs in retrieval._AREA_ALIASES_RAW[city].values() for v in vs}
    assert declared, f"no aliases declared for {city}"
    assert sorted(declared - real) == []


def test_row_satisfies_enforces_the_same_area_semantics():
    sq = StructuredQuery(city="Los Angeles", hard_constraints=["near Downtown LA"])
    parsed = retrieval._parse_constraints(sq)
    inside = _row("a", city="Los Angeles", neighbourhood="Downtown")
    outside = _row("b", city="Los Angeles", neighbourhood="Long Beach")
    assert retrieval._row_satisfies(sq, parsed, inside) is True
    assert retrieval._row_satisfies(sq, parsed, outside) is False


# ═════════════════════════════════════════════════════════════════════════════
# WS0-H — summaries collection is queried and fused
# ═════════════════════════════════════════════════════════════════════════════
def test_summaries_are_queried_with_query_points_and_the_same_vector(wired):
    wired["listing_hits"] = [_hit("a", 0.9)]
    wired["rows"] = {"a": _row("a")}

    asyncio.run(retrieval.retrieve(StructuredQuery(city="Lisbon"), limit=5))

    # exactly one embedding for the whole request — no double embed
    assert len(wired["embed_calls"]) == 1
    assert len(wired["query_points_kwargs"]) == 1
    kwargs = wired["query_points_kwargs"][0]
    assert kwargs["collection_name"] == "summaries"
    # same vector object the listings search used (query_vector vs query)
    assert kwargs["query"] == wired["search_kwargs"][0]["query_vector"]
    assert kwargs["with_payload"] is True


def test_summary_evidence_promotes_a_low_ranked_listing(wired):
    """EVAL failure #3: theme queries were decided by listing text alone."""
    # "a" is the top listing-text match; "d" is buried at listing rank 4 but is
    # the best guest-review-summary match.
    wired["listing_hits"] = [
        _hit("a", 0.90), _hit("b", 0.88), _hit("c", 0.86),
        _hit("d", 0.84), _hit("e", 0.82),
    ]
    wired["summary_points"] = [_hit("d", 0.77), _hit("e", 0.70)]
    wired["rows"] = {k: _row(k) for k in "abcde"}

    out = asyncio.run(retrieval.retrieve(StructuredQuery(city="Lisbon"), limit=3))
    ids = [card.id for card, _ in out]
    assert ids[0] == "d"          # in both lists → highest fused score
    assert "a" in ids             # strong single-list hits still survive
    # the rationale names both signals, and neither is invented
    rationale = dict((c.id, r) for c, r in out)["d"]
    assert "semantic match 0.84" in rationale
    assert "guest-review match 0.77" in rationale


def test_summary_only_candidate_is_admitted_when_it_meets_the_constraints(wired):
    wired["listing_hits"] = [_hit("a", 0.90)]
    wired["summary_points"] = [_hit("z", 0.95)]
    wired["rows"] = {"a": _row("a"), "z": _row("z", base_price=90.0)}

    out = asyncio.run(
        retrieval.retrieve(
            StructuredQuery(city="Lisbon", budget_per_night=120), limit=5
        )
    )
    ids = [c.id for c, _ in out]
    assert "z" in ids
    rationale = dict((c.id, r) for c, r in out)["z"]
    # summaries-only: it must NOT be presented as a listing-attribute match
    assert "guest-review match 0.95" in rationale
    assert "semantic match" not in rationale


@pytest.mark.parametrize(
    "bad_row,why",
    [
        (dict(city="Amsterdam"), "wrong city"),
        (dict(base_price=400.0), "over budget"),
        (dict(beds=1), "too few beds"),
        (dict(type="Private room"), "wrong property type"),
        (dict(amenities=["kitchen"]), "missing required amenity"),
        (dict(neighbourhood="Benfica"), "outside the requested area"),
    ],
)
def test_summary_only_candidates_cannot_bypass_hard_constraints(wired, bad_row, why):
    """The summaries payload is {listing_id} only, so Qdrant cannot filter it."""
    wired["listing_hits"] = [_hit("a", 0.90)]
    wired["summary_points"] = [_hit("z", 0.99)]
    wired["rows"] = {"a": _row("a"), "z": _row("z", **bad_row)}

    sq = StructuredQuery(
        city="Lisbon",
        budget_per_night=150,
        party_size=2,
        hard_constraints=["apartment", "wifi", "near Alfama"],
    )
    out = asyncio.run(retrieval.retrieve(sq, limit=5))
    assert [c.id for c, _ in out] == ["a"], why


def test_missing_summaries_collection_degrades_to_listings_only(wired):
    """A restored older snapshot may not carry the collection at all."""
    wired["summary_exc"] = RuntimeError(
        "Unexpected Response: 404 (Not Found) — Collection `summaries` doesn't exist!"
    )
    wired["listing_hits"] = [_hit("a", 0.90), _hit("b", 0.80), _hit("c", 0.70)]
    wired["rows"] = {k: _row(k) for k in "abc"}

    out = asyncio.run(retrieval.retrieve(StructuredQuery(city="Lisbon"), limit=5))
    # RRF over a single list is order-preserving → identical to the old path
    assert [c.id for c, _ in out] == ["a", "b", "c"]
    assert all("guest-review" not in r for _, r in out)
    # ...and the process stops retrying a collection that does not exist
    assert retrieval._summaries_missing is True


def test_transient_summaries_error_does_not_latch(wired):
    wired["summary_exc"] = TimeoutError("connection reset by peer")
    wired["listing_hits"] = [_hit("a", 0.90)]
    wired["rows"] = {"a": _row("a")}

    out = asyncio.run(retrieval.retrieve(StructuredQuery(city="Lisbon"), limit=5))
    assert [c.id for c, _ in out] == ["a"]
    assert retrieval._summaries_missing is False


def test_hydration_miss_is_dropped_not_fabricated(wired):
    """A vector hit whose Postgres row is gone must never become a card."""
    wired["listing_hits"] = [_hit("ghost", 0.99), _hit("a", 0.5)]
    wired["summary_points"] = [_hit("ghost", 0.99)]
    wired["rows"] = {"a": _row("a")}

    out = asyncio.run(retrieval.retrieve(StructuredQuery(city="Lisbon"), limit=5))
    assert [c.id for c, _ in out] == ["a"]


def test_limit_is_capped_and_overfetch_is_bounded(wired):
    wired["listing_hits"] = []
    wired["rows"] = {}
    asyncio.run(retrieval.retrieve(StructuredQuery(city="Lisbon"), limit=9999))
    assert wired["search_kwargs"][0]["limit"] == retrieval._MAX_LISTING_FETCH
    assert wired["query_points_kwargs"][0]["limit"] == retrieval._MAX_SUMMARY_FETCH


def test_over_constrained_search_still_relaxes(wired, monkeypatch):
    """The pre-existing empty-result fallback must survive the fusion rewrite."""
    calls = {"n": 0}

    class Client:
        async def search(self, **kwargs):
            calls["n"] += 1
            wired["search_kwargs"].append(kwargs)
            return [] if calls["n"] == 1 else [_hit("a", 0.4)]

        async def query_points(self, **kwargs):
            return types.SimpleNamespace(points=[])

    wired["rows"] = {"a": _row("a")}
    monkeypatch.setattr(retrieval, "get_qdrant", lambda: Client())

    sq = StructuredQuery(city="Lisbon", hard_constraints=["wifi", "near Alfama"])
    out = asyncio.run(retrieval.retrieve(sq, limit=5))
    assert calls["n"] == 2
    assert [c.id for c, _ in out] == ["a"]
    # the retry dropped the amenity + area terms but kept city
    relaxed = wired["search_kwargs"][-1]["query_filter"]
    assert _values(_conditions(relaxed, "amenities")) == []
    assert _values(_conditions(relaxed, "neighbourhood")) == []
    assert _values(_conditions(relaxed, "city")) == ["Lisbon"]
