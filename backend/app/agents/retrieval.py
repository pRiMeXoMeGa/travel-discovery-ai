"""Retrieval agent: semantic + filtered + area-constrained search.

Combines TWO Qdrant vector searches — the `listings` collection (listing
attribute text) and the `summaries` collection (per-property review-summary
text, WS0-H) — both with the SAME query vector (bge-small 384-dim, the same
model the corpus was embedded with, embedded once per request). Hard
constraints (price, beds, type, amenities, neighbourhood) are applied as
`listings` payload filters; the two result sets are fused by `listing_id`, then
hydrated from Postgres into a ranked candidate set with a short per-result
rationale (brief §2.3).

Area handling (WS0-E): "near <place>" phrases are resolved through a per-city
alias table onto the REAL `neighbourhood` values ingestion writes
(`neighbourhood_cleansed`), and applied as conditions on the already-indexed
`neighbourhood` keyword field. This is neighbourhood-granularity, not lat/lng
radius — there is deliberately no `geo` payload index, so no re-snapshot is
needed. Do not describe this as true geospatial search.

Grounding: rationales are built deterministically from real Postgres row fields
(price, rating, amenities, neighbourhood) — NOT free-text LLM generation — so a
rationale can never claim an attribute the listing does not have.

Memory: candidate sets are bounded by `limit` (default 20, hard cap 50); the
internal fusion pool is bounded by _MAX_LISTING_FETCH + _MAX_SUMMARY_FETCH.
"""
import asyncio
import logging
import re
import unicodedata
from typing import Any

from qdrant_client import models as qmodels

from ..cache import cache_get, cache_set
from ..db import get_pool
from ..embeddings import embed_query
from ..schemas import ListingCard, StructuredQuery
from ..vectorstore import get_qdrant
from ..config import settings

logger = logging.getLogger(__name__)

_HARD_CAP = 50

# ── Fusion tuning (WS0-H) ────────────────────────────────────────────────────
# Reciprocal Rank Fusion constant. 60 is the value from Cormack et al. (2009)
# and the Qdrant/Elastic default; it damps the head of each list so a rank-1
# hit does not automatically outrank a document that is strong in BOTH lists.
_FUSION_K = 60
# Both searches over-fetch so fusion has something to re-order. The summaries
# search over-fetches harder because it CANNOT be filtered (its payload is
# {"listing_id"} only), so most of its hits are for other cities/price bands
# and get dropped by the constraint re-check below.
_LISTING_OVERFETCH = 3
_SUMMARY_OVERFETCH = 5
_MAX_LISTING_FETCH = 100
_MAX_SUMMARY_FETCH = 150
# Upper bound on ids sent to the single hydration query.
_MAX_HYDRATE = 200

# Set once if the summaries collection is genuinely absent (e.g. an older
# restored snapshot) so we stop paying a round-trip per request. Transient
# errors do NOT latch this — they just degrade that one request.
_summaries_missing = False

# ── Amenity vocabulary (WS0-G / FINDINGS §2.10) ──────────────────────────────
# Single source of truth for the amenity vocabulary *inside the backend*.
#
# The authoritative list is `ingestion/enrich.py::CANONICAL_AMENITIES` — the 18
# terms `normalize_amenities()` writes into `listings.amenities` and the Qdrant
# payload. It is MIRRORED here rather than imported because `backend/Dockerfile`
# copies only `app/`; `ingestion/` is not on the backend's import path at
# runtime, so `from ingestion.enrich import CANONICAL_AMENITIES` would
# ImportError in the deployed image. `backend/tests/test_retrieval.py` asserts
# this tuple is byte-for-byte the ingestion list, so drift fails CI instead of
# silently producing filters that can never match.
CANONICAL_AMENITIES: tuple[str, ...] = (
    "wifi",
    "pool",
    "kitchen",
    "parking",
    "balcony",
    "ac",
    "gym",
    "washer",
    "pets_allowed",
    "hot_tub",
    "bbq",
    "workspace",
    "beach_access",
    "concierge",
    "breakfast_included",
    "ev_charger",
    "elevator",
    "baby_cot",
)
_KNOWN_AMENITIES = frozenset(CANONICAL_AMENITIES)

_KEY_AMENITY_PRIORITY = [
    "wifi", "pool", "hot_tub", "gym", "parking", "kitchen", "ac",
    "pets_allowed", "balcony", "breakfast_included", "workspace", "bbq",
]
# Map free-text type words → the real Inside Airbnb room_type values stored in
# listings.type (and the Qdrant payload). Keep keys lowercase.
_TYPE_KEYWORDS = {
    "apartment": "Entire home/apt",
    "flat": "Entire home/apt",
    "entire place": "Entire home/apt",
    "entire home": "Entire home/apt",
    "entire home/apt": "Entire home/apt",
    "whole place": "Entire home/apt",
    "house": "Entire home/apt",
    "private room": "Private room",
    "room": "Private room",
    "hotel": "Hotel room",
    "hotel room": "Hotel room",
    "shared room": "Shared room",
    "shared": "Shared room",
}

# ── Area alias table (WS0-E / FINDINGS §2.3) ─────────────────────────────────
# place phrase → the REAL `neighbourhood` values ingestion stores.
#
# Derivation: every right-hand value below was read out of
# `csvData/<city>/listings.csv` column `neighbourhood_cleansed`, which
# `ingestion/ingest.py:656-661` copies verbatim (`.strip()` only) into
# `listings.neighbourhood` and the Qdrant payload. Nothing here is guessed.
#
# ⚠ The Lisbon values look misspelled and are NOT typos. That CSV is
# accent-stripped at source: it literally contains "Misericrdia",
# "Santo Antnio", "So Vicente", "Belm", "Alcntara", "Parque das Naes"
# (verified: zero non-ASCII bytes in that column). Matching a filter against
# "Misericórdia" would return nothing. `_norm_place()` strips accents from the
# *user's* phrasing so "Belém" still resolves to the corpus's "Belm".
_AREA_ALIASES_RAW: dict[str, dict[str, tuple[str, ...]]] = {
    "Amsterdam": {
        "centre": ("Centrum-West", "Centrum-Oost"),
        "city centre": ("Centrum-West", "Centrum-Oost"),
        "downtown": ("Centrum-West", "Centrum-Oost"),
        "central": ("Centrum-West", "Centrum-Oost"),
        "old town": ("Centrum-West", "Centrum-Oost"),
        "centrum": ("Centrum-West", "Centrum-Oost"),
        "canal belt": ("Centrum-West", "Centrum-Oost"),
        "canals": ("Centrum-West", "Centrum-Oost"),
        "grachtengordel": ("Centrum-West", "Centrum-Oost"),
        "jordaan": ("Centrum-West",),
        "dam square": ("Centrum-West",),
        "anne frank house": ("Centrum-West",),
        "red light district": ("Centrum-Oost",),
        "de wallen": ("Centrum-Oost",),
        "central station": ("Centrum-West", "Centrum-Oost", "Oud-Noord"),
        "amsterdam centraal": ("Centrum-West", "Centrum-Oost", "Oud-Noord"),
        "station": ("Centrum-West", "Centrum-Oost", "Oud-Noord"),
        "metro": ("Centrum-West", "Centrum-Oost", "De Pijp - Rivierenbuurt", "Zuid"),
        "subway": ("Centrum-West", "Centrum-Oost", "De Pijp - Rivierenbuurt", "Zuid"),
        "tram": ("Centrum-West", "Centrum-Oost", "De Pijp - Rivierenbuurt", "Zuid"),
        "public transport": (
            "Centrum-West", "Centrum-Oost", "De Pijp - Rivierenbuurt", "Zuid",
        ),
        "de pijp": ("De Pijp - Rivierenbuurt",),
        "pijp": ("De Pijp - Rivierenbuurt",),
        "rivierenbuurt": ("De Pijp - Rivierenbuurt",),
        "oud west": ("De Baarsjes - Oud-West",),
        "de baarsjes": ("De Baarsjes - Oud-West",),
        "vondelpark": ("Zuid", "De Baarsjes - Oud-West"),
        "museum quarter": ("Zuid",),
        "museumplein": ("Zuid",),
        "rijksmuseum": ("Zuid",),
        "van gogh museum": ("Zuid",),
        "zuid": ("Zuid",),
        "south": ("Zuid", "Buitenveldert - Zuidas"),
        "zuidas": ("Buitenveldert - Zuidas",),
        "business district": ("Buitenveldert - Zuidas",),
        "rai": ("Buitenveldert - Zuidas", "De Pijp - Rivierenbuurt"),
        "noord": ("Oud-Noord", "Noord-West", "Noord-Oost"),
        "north": ("Oud-Noord", "Noord-West", "Noord-Oost"),
        "ndsm": ("Oud-Noord", "Noord-West"),
        "oost": ("Oud-Oost", "Oostelijk Havengebied - Indische Buurt", "Watergraafsmeer"),
        "east": ("Oud-Oost", "Oostelijk Havengebied - Indische Buurt", "Watergraafsmeer"),
        "west": ("De Baarsjes - Oud-West", "Westerpark", "Bos en Lommer"),
        "westerpark": ("Westerpark",),
        "bos en lommer": ("Bos en Lommer",),
        "ijburg": ("IJburg - Zeeburgereiland",),
        "watergraafsmeer": ("Watergraafsmeer",),
        "schiphol": ("Buitenveldert - Zuidas", "Slotervaart", "Zuid"),
        "airport": ("Buitenveldert - Zuidas", "Slotervaart", "Zuid"),
    },
    "Lisbon": {
        "centre": ("Santa Maria Maior", "Misericrdia", "Santo Antnio"),
        "city centre": ("Santa Maria Maior", "Misericrdia", "Santo Antnio"),
        "downtown": ("Santa Maria Maior", "Misericrdia", "Santo Antnio"),
        "central": ("Santa Maria Maior", "Misericrdia", "Santo Antnio"),
        "old town": ("Santa Maria Maior", "So Vicente"),
        "historic centre": ("Santa Maria Maior", "So Vicente", "Misericrdia"),
        "baixa": ("Santa Maria Maior",),
        "rossio": ("Santa Maria Maior",),
        "alfama": ("Santa Maria Maior",),
        "mouraria": ("Santa Maria Maior", "So Vicente"),
        "castle": ("Santa Maria Maior",),
        "chiado": ("Misericrdia",),
        "bairro alto": ("Misericrdia",),
        "cais do sodre": ("Misericrdia",),
        "principe real": ("Misericrdia", "Santo Antnio"),
        "graca": ("So Vicente",),
        "sao vicente": ("So Vicente",),
        "avenida": ("Santo Antnio",),
        "avenida da liberdade": ("Santo Antnio",),
        "marques de pombal": ("Santo Antnio", "Avenidas Novas"),
        "saldanha": ("Avenidas Novas",),
        "avenidas novas": ("Avenidas Novas",),
        "arroios": ("Arroios",),
        "anjos": ("Arroios",),
        "intendente": ("Arroios",),
        "estrela": ("Estrela",),
        "lapa": ("Estrela",),
        "campo de ourique": ("Campo de Ourique",),
        "belem": ("Belm",),
        "jeronimos": ("Belm",),
        "alcantara": ("Alcntara",),
        "lx factory": ("Alcntara",),
        "docas": ("Alcntara",),
        "parque das nacoes": ("Parque das Naes",),
        "expo": ("Parque das Naes",),
        "oceanarium": ("Parque das Naes",),
        "alvalade": ("Alvalade",),
        "areeiro": ("Areeiro",),
        "campolide": ("Campolide",),
        "penha de franca": ("Penha de Frana",),
        "metro": (
            "Santa Maria Maior", "Santo Antnio", "Avenidas Novas", "Arroios",
            "Areeiro", "Alvalade",
        ),
        "subway": (
            "Santa Maria Maior", "Santo Antnio", "Avenidas Novas", "Arroios",
            "Areeiro", "Alvalade",
        ),
        "public transport": (
            "Santa Maria Maior", "Santo Antnio", "Avenidas Novas", "Arroios",
            "Areeiro", "Alvalade",
        ),
        "airport": ("Olivais", "Alvalade", "Areeiro", "Parque das Naes"),
        "beach": (
            "Cascais e Estoril", "Carcavelos e Parede", "Ericeira", "Colares",
            "Santo Isidoro",
            "Algs, Linda-a-Velha e Cruz Quebrada-Dafundo",
        ),
        "coast": (
            "Cascais e Estoril", "Carcavelos e Parede", "Ericeira", "Colares",
            "Santo Isidoro",
        ),
        "seaside": ("Cascais e Estoril", "Carcavelos e Parede", "Ericeira"),
        "cascais": ("Cascais e Estoril",),
        "estoril": ("Cascais e Estoril",),
        "carcavelos": ("Carcavelos e Parede",),
        "ericeira": ("Ericeira",),
        "sintra": (
            "S.Maria, S.Miguel, S.Martinho, S.Pedro Penaferrim",
            "Colares",
            "So Joo das Lampas e Terrugem",
            "Algueiro-Mem Martins",
            "Rio de Mouro",
        ),
    },
    "Los Angeles": {
        # EVAL failure #2: "near Downtown LA" previously drifted to Long Beach.
        "downtown": ("Downtown",),
        "downtown la": ("Downtown",),
        "downtown los angeles": ("Downtown",),
        "dtla": ("Downtown",),
        "centre": ("Downtown",),
        "city centre": ("Downtown",),
        "central": ("Downtown",),
        "financial district": ("Downtown",),
        "arts district": ("Downtown",),
        "little tokyo": ("Downtown",),
        "chinatown": ("Chinatown",),
        "hollywood": (
            "Hollywood", "East Hollywood", "Hollywood Hills",
            "Hollywood Hills West", "West Hollywood",
        ),
        "west hollywood": ("West Hollywood",),
        "weho": ("West Hollywood",),
        "sunset strip": ("West Hollywood",),
        "hollywood hills": ("Hollywood Hills", "Hollywood Hills West"),
        "walk of fame": ("Hollywood",),
        "griffith park": ("Los Feliz", "Hollywood Hills", "Atwater Village"),
        "observatory": ("Los Feliz", "Hollywood Hills"),
        "los feliz": ("Los Feliz",),
        "silver lake": ("Silver Lake",),
        "silverlake": ("Silver Lake",),
        "echo park": ("Echo Park",),
        "koreatown": ("Koreatown",),
        "k town": ("Koreatown",),
        "beverly hills": ("Beverly Hills", "Beverly Grove", "Beverly Crest"),
        "rodeo drive": ("Beverly Hills",),
        "santa monica": ("Santa Monica",),
        "santa monica pier": ("Santa Monica",),
        "venice": ("Venice",),
        "venice beach": ("Venice",),
        "abbot kinney": ("Venice",),
        "marina del rey": ("Marina del Rey",),
        "culver city": ("Culver City",),
        "long beach": ("Long Beach",),
        "malibu": ("Malibu",),
        "pasadena": ("Pasadena", "South Pasadena", "East Pasadena"),
        "westwood": ("Westwood",),
        "ucla": ("Westwood",),
        "brentwood": ("Brentwood",),
        "usc": ("Exposition Park",),
        "exposition park": ("Exposition Park",),
        "beach": (
            "Venice", "Santa Monica", "Manhattan Beach", "Hermosa Beach",
            "Redondo Beach", "Marina del Rey", "Playa del Rey", "Malibu",
            "Long Beach",
        ),
        "coast": (
            "Venice", "Santa Monica", "Manhattan Beach", "Hermosa Beach",
            "Redondo Beach", "Marina del Rey", "Playa del Rey", "Malibu",
        ),
        "beachfront": (
            "Venice", "Santa Monica", "Manhattan Beach", "Hermosa Beach",
            "Redondo Beach", "Playa del Rey", "Malibu",
        ),
        "south bay": (
            "Manhattan Beach", "Hermosa Beach", "Redondo Beach", "El Segundo",
            "Torrance",
        ),
        "lax": ("Westchester", "Inglewood", "El Segundo", "Playa del Rey"),
        "airport": ("Westchester", "Inglewood", "El Segundo", "Playa del Rey"),
        "the valley": (
            "Sherman Oaks", "Studio City", "North Hollywood", "Van Nuys",
            "Encino", "Tarzana", "Woodland Hills", "Valley Village",
            "Valley Glen", "Reseda", "Northridge", "Canoga Park", "Winnetka",
            "North Hills", "Lake Balboa", "Sun Valley", "Granada Hills",
            "Chatsworth", "West Hills", "Toluca Lake",
        ),
        "san fernando valley": (
            "Sherman Oaks", "Studio City", "North Hollywood", "Van Nuys",
            "Encino", "Tarzana", "Woodland Hills", "Valley Village",
            "Valley Glen", "Reseda", "Northridge", "Canoga Park", "Winnetka",
            "North Hills", "Lake Balboa", "Sun Valley", "Granada Hills",
            "Chatsworth", "West Hills", "Toluca Lake",
        ),
        "studio city": ("Studio City",),
        "sherman oaks": ("Sherman Oaks",),
        "north hollywood": ("North Hollywood",),
        "noho": ("North Hollywood",),
        "burbank": ("Burbank",),
        "glendale": ("Glendale",),
        "universal studios": ("Studio City", "North Hollywood", "Toluca Lake"),
        "san pedro": ("San Pedro",),
        "metro": ("Downtown", "Koreatown", "Hollywood", "Westlake", "Chinatown"),
        "subway": ("Downtown", "Koreatown", "Hollywood", "Westlake", "Chinatown"),
        "public transport": (
            "Downtown", "Koreatown", "Hollywood", "Westlake", "Chinatown",
        ),
    },
}

_ARTICLE_RE = re.compile(r"^(the|a|an)\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _norm_place(text: str) -> str:
    """Fold a place phrase to a comparable key.

    Lower-cases, strips diacritics (the corpus is accent-stripped — see the
    alias table comment), collapses punctuation/whitespace, and drops a leading
    article so "the Metro" and "Metro" hit the same key.
    """
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    collapsed = _NON_ALNUM_RE.sub(" ", ascii_only).strip()
    return _ARTICLE_RE.sub("", collapsed).strip()


# Normalized lookup: city → [(alias_key, values)], longest alias first so
# "west hollywood" wins over "west" and "downtown la" over "downtown".
_AREA_ALIASES: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    city: sorted(
        ((_norm_place(alias), values) for alias, values in table.items()),
        key=lambda kv: len(kv[0]),
        reverse=True,
    )
    for city, table in _AREA_ALIASES_RAW.items()
}


def _resolve_place(city: str | None, phrase: str) -> tuple[str, ...]:
    """Resolve one place phrase to real `neighbourhood` values for `city`.

    Returns () when the city is unknown or nothing matches — an unresolvable
    place must NOT become a filter, or we would silently exclude everything.
    The phrase still reaches the semantic query text via `_query_text`.
    """
    table = _AREA_ALIASES.get(city or "")
    if not table:
        return ()
    key = _norm_place(phrase)
    if not key:
        return ()
    padded = f" {key} "
    for alias, values in table:  # longest-first
        if alias == key or f" {alias} " in padded:
            return values
    return ()


def _resolve_areas(city: str | None, phrases: list[str]) -> tuple[str, ...]:
    """Union of resolved neighbourhood values, order-preserved and deduped."""
    out: dict[str, None] = {}
    for phrase in phrases:
        for value in _resolve_place(city, phrase):
            out[value] = None
    return tuple(out)


def _avoid_values(city: str | None, phrases: list[str]) -> tuple[str, ...]:
    """Neighbourhood values to exclude.

    Prefers the alias table (so "avoid de pijp" excludes the real
    "De Pijp - Rivierenbuurt"); falls back to the pre-WS0-E `.title()` guess so
    a bare neighbourhood name the table does not carry still works.
    """
    out: dict[str, None] = {}
    for phrase in phrases:
        resolved = _resolve_place(city, phrase)
        for value in resolved or (phrase.title(),):
            out[value] = None
    return tuple(out)


def _normalize_amenity(token: str) -> str | None:
    t = token.lower().strip().replace(" ", "_").replace("-", "_")
    if t in _KNOWN_AMENITIES:
        return t
    # a few synonyms
    synonyms = {
        "air_conditioning": "ac", "aircon": "ac", "swimming_pool": "pool",
        "pet_friendly": "pets_allowed", "pets": "pets_allowed",
        "wi_fi": "wifi", "internet": "wifi", "car_park": "parking",
        "beach": "beach_access",
        # baby_cot is in the canonical vocabulary; these are how users say it.
        "cot": "baby_cot", "crib": "baby_cot", "baby_crib": "baby_cot",
        "travel_cot": "baby_cot",
    }
    return synonyms.get(t)


# Prefixes that introduce a proximity constraint. Prefix-anchored on purpose:
# the previous `low.replace("near", "")` also mangled words like "nearest".
_NEAR_PREFIXES = (
    "near ", "close to ", "next to ", "nearby ",
    "walking distance to ", "walking distance from ", "walkable to ",
)


def _parse_constraints(sq: StructuredQuery) -> dict[str, Any]:
    """Translate the StructuredQuery into structured filter primitives.

    Returns dict with keys: amenities(list), avoid_areas(list[str]),
    near_areas(list[str]), property_type(str|None).

    NOTE: name, signature and returned keys are a public contract —
    `app/routers/agents.py::parse_and_search` imports this directly.
    """
    amenities: list[str] = []
    avoid_areas: list[str] = []
    near_areas: list[str] = []
    property_type: str | None = None

    for raw in [*sq.hard_constraints, *sq.soft_preferences]:
        low = raw.lower().strip()
        # area avoidance / inclusion
        if low.startswith("avoid ") or low.startswith("not "):
            avoid_areas.append(low.split(" ", 1)[1].strip())
            continue
        for prefix in _NEAR_PREFIXES:
            if low.startswith(prefix):
                near_areas.append(low[len(prefix):].strip())
                break
        # property type
        for kw, canonical in _TYPE_KEYWORDS.items():
            if kw in low:
                property_type = canonical
        # amenity
        am = _normalize_amenity(low)
        if am:
            amenities.append(am)

    # dedupe preserving order
    amenities = list(dict.fromkeys(amenities))
    return {
        "amenities": amenities,
        "avoid_areas": list(dict.fromkeys(avoid_areas)),
        "near_areas": list(dict.fromkeys(near_areas)),
        "property_type": property_type,
    }


def _build_qdrant_filter(sq: StructuredQuery, parsed: dict) -> qmodels.Filter | None:
    """Build a Qdrant payload filter from hard constraints.

    Listings payload fields: listing_id, name, type, city, neighbourhood, lat,
    lng, base_price, beds, rating, amenities.

    Every field used here is in the keyword/float/integer index set created by
    `scripts/ensure_qdrant_indexes.sh` (city, type, neighbourhood, amenities,
    base_price, beds). Qdrant Cloud runs STRICT mode: filtering an un-indexed
    field 400s, so do not add a condition on a field that script does not index.
    """
    must: list[qmodels.Condition] = []
    must_not: list[qmodels.Condition] = []

    if sq.city:
        must.append(
            qmodels.FieldCondition(
                key="city", match=qmodels.MatchValue(value=sq.city)
            )
        )
    price_cap = sq.budget_per_night
    if price_cap is not None:
        must.append(
            qmodels.FieldCondition(key="base_price", range=qmodels.Range(lte=price_cap))
        )
    if sq.party_size and sq.party_size > 1:
        must.append(
            qmodels.FieldCondition(key="beds", range=qmodels.Range(gte=sq.party_size))
        )
    if parsed["property_type"]:
        must.append(
            qmodels.FieldCondition(
                key="type", match=qmodels.MatchValue(value=parsed["property_type"])
            )
        )
    for am in parsed["amenities"]:
        # amenities stored as a JSON array in the payload; MatchValue on an
        # array field matches when the value is a member.
        must.append(
            qmodels.FieldCondition(key="amenities", match=qmodels.MatchValue(value=am))
        )

    # WS0-E: "near <place>" → OR over the real neighbourhood values for that
    # place. Expressed as a nested Filter(should=…) inside `must` rather than
    # MatchAny so the condition composes with the other `must` entries as a
    # single AND term. Over-constraining is safe: `retrieve()` retries without
    # amenity/area filters when the filtered search comes back empty.
    near_values = _resolve_areas(sq.city, parsed["near_areas"])
    if near_values:
        must.append(
            qmodels.Filter(
                should=[
                    qmodels.FieldCondition(
                        key="neighbourhood", match=qmodels.MatchValue(value=v)
                    )
                    for v in near_values
                ]
            )
        )

    for value in _avoid_values(sq.city, parsed["avoid_areas"]):
        must_not.append(
            qmodels.FieldCondition(
                key="neighbourhood", match=qmodels.MatchValue(value=value)
            )
        )

    if not must and not must_not:
        return None
    return qmodels.Filter(must=must or None, must_not=must_not or None)


def _row_satisfies(sq: StructuredQuery, parsed: dict, row: Any) -> bool:
    """Constraint re-check for candidates that never passed Qdrant's filter.

    Summaries-collection hits carry only {"listing_id"}, so the vector store
    cannot filter them — without this they would smuggle wrong-city / over-budget
    listings into a constrained result set. This mirrors `_build_qdrant_filter`
    against the hydrated Postgres row (the source of truth), reusing the SAME
    `_resolve_areas`/`_avoid_values` helpers so area semantics cannot drift.

    It is a filter only: it can drop a candidate, never invent or alter one.
    """
    if sq.city and (row["city"] or "") != sq.city:
        return False
    if sq.budget_per_night is not None:
        price = row["base_price"]
        if price is None or float(price) > float(sq.budget_per_night):
            return False
    if sq.party_size and sq.party_size > 1:
        if (row["beds"] or 0) < sq.party_size:
            return False
    if parsed["property_type"] and row["type"] != parsed["property_type"]:
        return False
    if parsed["amenities"]:
        have = set(row["amenities"] or [])
        if any(a not in have for a in parsed["amenities"]):
            return False
    neighbourhood = row["neighbourhood"]
    near_values = _resolve_areas(sq.city, parsed["near_areas"])
    if near_values and neighbourhood not in near_values:
        return False
    if neighbourhood in _avoid_values(sq.city, parsed["avoid_areas"]):
        return False
    return True


def _query_text(sq: StructuredQuery) -> str:
    """Compose the semantic query string from the structured intent."""
    parts: list[str] = []
    if sq.vibe:
        parts.append(sq.vibe)
    parts.extend(sq.soft_preferences)
    parts.extend(sq.hard_constraints)
    if sq.city:
        parts.append(f"in {sq.city}")
    if not parts:
        parts.append(sq.city or "place to stay")
    return " ".join(parts)


def _rationale(
    evidence: dict,
    parsed: dict,
    listing_score: float | None,
    summary_score: float | None,
) -> str:
    """Deterministic, grounded rationale from the hydrated Postgres row.

    The score clause names which vector space produced the match, so a result
    surfaced purely by guest-review summaries is never presented as a listing
    attribute match.
    """
    bits: list[str] = []
    matched = [a for a in parsed["amenities"] if a in (evidence.get("amenities") or [])]
    if matched:
        bits.append("has " + ", ".join(matched))
    rating = evidence.get("rating")
    if rating:
        bits.append(f"rated {round(float(rating), 2)}")
    nb = evidence.get("neighbourhood")
    if nb:
        bits.append(f"in {nb}")
    price = evidence.get("base_price")
    if price is not None:
        bits.append(f"${round(float(price))}/night")
    if listing_score is not None and summary_score is not None:
        bits.append(
            f"semantic match {listing_score:.2f}; guest-review match {summary_score:.2f}"
        )
    elif listing_score is not None:
        bits.append(f"semantic match {listing_score:.2f}")
    elif summary_score is not None:
        bits.append(f"guest-review match {summary_score:.2f}")
    return "; ".join(bits)


async def _hydrate(listing_ids: list[str]) -> dict[str, Any]:
    """Fetch full listing rows from Postgres keyed by id (single query)."""
    if not listing_ids:
        return {}
    placeholders = ", ".join(f"${i + 1}" for i in range(len(listing_ids)))
    sql = (
        "SELECT id, name, type, city, neighbourhood, lat, lng, base_price, beds, "
        "amenities, photos, rating, review_count "
        f"FROM listings WHERE id IN ({placeholders})"
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *listing_ids)
    return {r["id"]: r for r in rows}


def _cache_key(sq: StructuredQuery, limit: int) -> str:
    import hashlib, json
    raw = json.dumps(sq.model_dump(mode="json"), sort_keys=True, default=str)
    return "retrieval:" + hashlib.sha256((raw + f"|{limit}").encode()).hexdigest()


def _looks_like_missing_collection(exc: Exception) -> bool:
    """True when the error says the collection does not exist (not a blip)."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in ("not found", "doesn't exist", "does not exist", "404")
    )


async def _search_summaries(client, vector: list[float], limit: int) -> list[tuple[str, float]]:
    """Query the review-summary vectors. Returns [(listing_id, score)] by rank.

    Never raises: a missing or unhealthy summaries collection degrades the
    request to the listings-only path (a restored older snapshot may not carry
    the collection at all). Uses `query_points` — `.search` is deprecated.
    """
    global _summaries_missing
    if _summaries_missing:
        return []
    try:
        response = await client.query_points(
            collection_name=settings.qdrant_collection_summaries,
            query=vector,
            limit=limit,
            with_payload=True,
        )
    except Exception as exc:  # noqa: BLE001 — summaries are an optional signal
        if _looks_like_missing_collection(exc):
            _summaries_missing = True
            logger.warning(
                "qdrant collection %r absent — retrieval degrades to "
                "listings-only for this process: %s",
                settings.qdrant_collection_summaries,
                exc,
            )
        else:
            # Several qdrant/httpx transport errors stringify to "", so the type
            # name is the only clue this path leaves behind. Log both — this
            # branch degrades silently by design, and an empty warning line
            # makes a real outage indistinguishable from a warm-up blip.
            logger.warning(
                "summaries search failed (degrading to listings-only): %s: %s",
                type(exc).__name__,
                exc,
            )
        return []

    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for point in getattr(response, "points", None) or []:
        payload = getattr(point, "payload", None) or {}
        lid = payload.get("listing_id")
        if not lid or lid in seen:
            continue
        seen.add(lid)
        out.append((str(lid), float(getattr(point, "score", 0.0) or 0.0)))
    return out


async def retrieve(
    sq: StructuredQuery, limit: int = 20
) -> list[tuple[ListingCard, str]]:
    """Return ranked (ListingCard, rationale) candidates.

    Pipeline:
      1. embed the composed query intent ONCE (bge-small).
      2. with that one vector, search `listings` (hard constraints applied as
         payload filters) and `summaries` (per-property review-summary vectors)
         concurrently.
      3. fuse the two rankings by `listing_id` with Reciprocal Rank Fusion.
      4. hydrate full rows from Postgres; re-check constraints for any candidate
         that only came from `summaries`; build grounded rationales.

    Why RRF and not max-score: the two collections embed very different text
    (a short attribute string vs. review prose) with the same encoder, so their
    cosine distributions are not on a comparable scale — max-score would let
    whichever collection happens to produce larger absolute similarities decide
    the whole ordering. RRF uses ranks only, so it is scale-free, and a listing
    that is strong in BOTH lists outranks one that is strong in either alone.
    With a single non-empty list RRF is order-preserving, so the summaries path
    failing reproduces the previous listings-only ordering exactly.

    If the filtered semantic search yields nothing (over-constrained), retry once
    without amenity/area filters so we degrade to a looser but still city/price
    bounded result rather than returning empty.
    """
    limit = max(1, min(limit, _HARD_CAP))

    key = _cache_key(sq, limit)
    try:
        cached = await cache_get(key)
        if cached:
            return [(ListingCard(**c), r) for c, r in cached]
    except Exception as exc:  # noqa: BLE001 — cache is best-effort
        logger.warning("retrieval cache_get failed: %s", exc)

    parsed = _parse_constraints(sq)
    qtext = _query_text(sq)
    vector = await embed_query(qtext)  # embedded once; reused by both searches
    client = get_qdrant()

    listings_n = min(limit * _LISTING_OVERFETCH, _MAX_LISTING_FETCH)
    summaries_n = min(limit * _SUMMARY_OVERFETCH, _MAX_SUMMARY_FETCH)

    async def _search(qfilter):
        return await client.search(
            collection_name=settings.qdrant_collection_listings,
            query_vector=vector,
            query_filter=qfilter,
            limit=listings_n,
            with_payload=True,
        )

    qfilter = _build_qdrant_filter(sq, parsed)
    hits, summary_hits = await asyncio.gather(
        _search(qfilter),
        _search_summaries(client, vector, summaries_n),
    )

    # Over-constrained fallback: relax amenity + area filters, keep city/price.
    if not hits and qfilter is not None:
        relaxed = StructuredQuery(
            city=sq.city,
            party_size=sq.party_size,
            budget_per_night=sq.budget_per_night,
        )
        loose = _build_qdrant_filter(relaxed, _parse_constraints(relaxed))
        hits = await _search(loose)

    # ── Reciprocal Rank Fusion by listing_id ────────────────────────────────
    fused: dict[str, dict[str, Any]] = {}
    for rank, hit in enumerate(hits):
        payload = getattr(hit, "payload", None) or {}
        lid = payload.get("listing_id")
        if not lid:
            continue
        entry = fused.setdefault(
            lid, {"rrf": 0.0, "listing_score": None, "summary_score": None}
        )
        if entry["listing_score"] is None:  # first (best) rank for this id wins
            entry["rrf"] += 1.0 / (_FUSION_K + rank + 1)
            entry["listing_score"] = float(getattr(hit, "score", 0.0) or 0.0)
    for rank, (lid, score) in enumerate(summary_hits):
        entry = fused.setdefault(
            lid, {"rrf": 0.0, "listing_score": None, "summary_score": None}
        )
        if entry["summary_score"] is None:
            entry["rrf"] += 1.0 / (_FUSION_K + rank + 1)
            entry["summary_score"] = score

    ordered = sorted(fused.items(), key=lambda kv: kv[1]["rrf"], reverse=True)
    ordered = ordered[:_MAX_HYDRATE]
    rows = await _hydrate([lid for lid, _ in ordered])

    results: list[tuple[ListingCard, str]] = []
    for lid, entry in ordered:
        if len(results) >= limit:
            break
        row = rows.get(lid)
        if row is None:
            continue
        # Candidates that only came from `summaries` never met the payload
        # filter — enforce the same constraints here or they leak through.
        if entry["listing_score"] is None and not _row_satisfies(sq, parsed, row):
            continue
        amenities = list(row["amenities"]) if row["amenities"] else []
        photos = list(row["photos"]) if row["photos"] else []
        card = ListingCard(
            id=row["id"],
            name=row["name"],
            type=row["type"],
            city=row["city"],
            neighbourhood=row["neighbourhood"],
            lat=float(row["lat"]),
            lng=float(row["lng"]),
            price_per_night=float(row["base_price"]),
            rating=float(row["rating"]) if row["rating"] is not None else None,
            review_count=row["review_count"] or 0,
            key_amenities=[a for a in _KEY_AMENITY_PRIORITY if a in amenities][:4],
            photo=photos[0] if photos else None,
        )
        evidence = {
            "amenities": amenities,
            "rating": row["rating"],
            "neighbourhood": row["neighbourhood"],
            "base_price": row["base_price"],
        }
        results.append(
            (
                card,
                _rationale(
                    evidence, parsed, entry["listing_score"], entry["summary_score"]
                ),
            )
        )

    try:
        await cache_set(
            key,
            [(c.model_dump(mode="json"), r) for c, r in results],
            ttl=300,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("retrieval cache_set failed: %s", exc)

    return results
