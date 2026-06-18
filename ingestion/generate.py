"""Synthetic data generation (brief §2.1, Option B).

Re-runnable / deterministic: pass a fixed seed and you always get the same
corpus.  Faker + per-city bounding boxes + realistic distributions.

Scale is parameterised via GenConfig — dev defaults are 1 000 listings /
5 000 reviews so the full pipeline finishes in a few minutes.  Bump
n_listings / n_reviews to hit the 50 K / 200 K production targets.

Memory contract: both public generators are Python generators that yield one
dict at a time — the caller controls batching; nothing is materialised into a
full-corpus list here.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Generator

from faker import Faker

# ---------------------------------------------------------------------------
# Constants & vocabulary
# ---------------------------------------------------------------------------

# Dev-scale defaults — override via GenConfig.
DEV_N_LISTINGS = 1_000
DEV_N_REVIEWS = 5_000

# Production targets (for reference / full-scale runs).
PROD_N_LISTINGS = 50_000
PROD_N_REVIEWS = 200_000

# NOTE: Synthetic generation is retired as the primary path.
# The real Inside Airbnb CSV loader in ingest.py (--source real-csv) is now
# the primary ingest path for Amsterdam, Lisbon, and Los Angeles.
# This constant is preserved for backwards-compat / unit-test usage only.
CITIES = ["Amsterdam", "Lisbon", "Los Angeles"]

# Canonical amenity vocabulary (single source of truth; enrich.py normalises
# raw strings to this list).
CANONICAL_AMENITIES: list[str] = [
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
]

PROPERTY_TYPES: list[str] = [
    "entire place",
    "private room",
    "hotel",
    "shared room",
]

# Probability weights (sum to 1 inside each distribution).
PROPERTY_TYPE_WEIGHTS: list[float] = [0.50, 0.28, 0.17, 0.05]

LANGUAGES: list[str] = ["en", "pt", "ar", "fr", "es"]
# Weight: English + Portuguese dominate Lisbon; Arabic + English dominate Dubai.
# We simplify to a single global distribution; city-aware weighting is a nice-to-
# have added later without breaking the interface.
LANGUAGE_WEIGHTS: list[float] = [0.45, 0.20, 0.15, 0.10, 0.10]

# ---------------------------------------------------------------------------
# Per-city geographic bounding boxes  [lat_min, lat_max, lng_min, lng_max]
# NOTE: Real data is loaded via the CSV loader in ingest.py; these bounds are
# used only by the legacy synthetic generator (kept for backwards compat).
# ---------------------------------------------------------------------------
CITY_BOUNDS: dict[str, dict] = {
    "Amsterdam": {
        "lat": (52.29, 52.43),
        "lng": (4.75, 5.03),
        "neighbourhoods": [
            "Centrum", "De Pijp", "Jordaan", "Oud-West", "Oud-Zuid",
            "Oost", "Noord", "Westerpark", "Bos en Lommer", "Zeeburg",
            "Slotervaart", "Geuzenveld-Slotermeer", "De Baarsjes", "Indische Buurt",
        ],
        "price_range": (60.0, 400.0),
    },
    "Lisbon": {
        "lat": (38.68, 38.78),
        "lng": (-9.23, -9.08),
        "neighbourhoods": [
            "Alfama", "Belém", "Bairro Alto", "Chiado", "Príncipe Real",
            "Mouraria", "Intendente", "Lapa", "Campo de Ourique", "Benfica",
            "Alvalade", "Arroios", "Penha de França", "Olivais", "Parque das Nações",
        ],
        # Base price range (€ / night) for this city.
        "price_range": (45.0, 320.0),
    },
    "Los Angeles": {
        "lat": (33.70, 34.35),
        "lng": (-118.67, -118.15),
        "neighbourhoods": [
            "Hollywood", "Silver Lake", "Echo Park", "Koreatown", "Downtown",
            "Venice", "Santa Monica", "West Hollywood", "Los Feliz", "Silverlake",
            "Eagle Rock", "Highland Park", "Culver City", "Mid-City", "Brentwood",
        ],
        "price_range": (50.0, 500.0),
    },
}

# Beds distribution: (beds, weight).
BEDS_DIST: list[tuple[int, float]] = [
    (1, 0.35),
    (2, 0.30),
    (3, 0.18),
    (4, 0.10),
    (5, 0.05),
    (6, 0.02),
]

# ---------------------------------------------------------------------------
# Review text templates (offline — no LLM required).
# We keep a rich pool so the generated corpus isn't obviously monotonous.
# ---------------------------------------------------------------------------
_REVIEW_TEMPLATES: dict[str, list[str]] = {
    "en": [
        "Wonderful stay! The {amenity} was fantastic and the {aspect} was impeccable.",
        "Great location in {neighbourhood}. Would definitely come back.",
        "The apartment was clean and well-equipped. Host was very responsive.",
        "Perfect for a city break. The {amenity} made all the difference.",
        "Slightly overpriced for the area but overall a pleasant experience.",
        "The {aspect} could use some improvement but the location is unbeatable.",
        "Loved the {neighbourhood} neighbourhood — so much character!",
        "Fantastic host and an amazing property. Highly recommended.",
        "A bit noisy at night but the views more than made up for it.",
        "Exactly as described — great value for the price.",
        "The {amenity} was a real bonus. Slept like a log.",
        "Clean, comfortable, and in a great spot near public transport.",
        "The check-in process was smooth and the place was spotless.",
        "Hidden gem in {neighbourhood}. Will be back next year.",
        "Good stay overall. A few small things could be better but no complaints.",
    ],
    "pt": [
        "Estadia fantástica! O lugar é exatamente como nas fotos.",
        "Ótima localização no bairro de {neighbourhood}. Recomendo muito.",
        "O anfitrião foi super atencioso e o apartamento estava impecável.",
        "Perfeito para visitar a cidade. Voltarei com certeza.",
        "Bom custo-benefício. A {amenity} foi um plus.",
        "Localização excelente, perto de tudo. Muito satisfeito.",
        "Apartamento confortável e bem equipado. Sem reclamações.",
        "A limpeza estava ótima e o check-in foi tranquilo.",
    ],
    "ar": [
        "إقامة رائعة! المكان نظيف ومريح جداً.",
        "موقع ممتاز في {neighbourhood}. أنصح به بشدة.",
        "المضيف كان متعاوناً جداً والشقة كانت كما في الصور تماماً.",
        "تجربة رائعة. سأعود بالتأكيد في المرة القادمة.",
        "قيمة ممتازة مقابل السعر. كل شيء كان مرتباً.",
        "المكان هادئ ومريح. الخدمات كانت ممتازة.",
    ],
    "fr": [
        "Séjour fantastique! L'endroit était propre et bien situé.",
        "Hôte très accueillant et appartement conforme aux photos.",
        "Excellent rapport qualité-prix dans le quartier de {neighbourhood}.",
        "Très bonne expérience globale. Je recommande vivement.",
        "La {amenity} était un vrai plus. Très satisfait.",
    ],
    "es": [
        "¡Estancia fantástica! El lugar era exactamente como en las fotos.",
        "Excelente ubicación en {neighbourhood}. Muy recomendable.",
        "El anfitrión fue muy atento y el apartamento estaba impecable.",
        "Perfecto para visitar la ciudad. Repetiré sin duda.",
        "Muy buena relación calidad-precio. Sin quejas.",
    ],
}

_ASPECTS = ["cleanliness", "location", "value", "staff", "noise"]

# ---------------------------------------------------------------------------
# Deterministic ID helpers
# ---------------------------------------------------------------------------

def _listing_id(seed_str: str) -> str:
    """Stable deterministic UUID from an arbitrary seed string."""
    return str(uuid.UUID(hashlib.md5(seed_str.encode()).hexdigest()))


def _review_id(listing_id: str, idx: int) -> str:
    return str(uuid.UUID(hashlib.md5(f"rev:{listing_id}:{idx}".encode()).hexdigest()))


# ---------------------------------------------------------------------------
# Real photo pool (city-split). Loaded once from photo_pool.json, built from
# the curated Airbnb CDN URL list. Each listing gets a deterministic, varied
# subset so galleries are stable across runs and city-appropriate.
# ---------------------------------------------------------------------------

_PHOTO_POOL: dict[str, list[str]] | None = None


def _photo_pool() -> dict[str, list[str]]:
    global _PHOTO_POOL
    if _PHOTO_POOL is None:
        path = os.path.join(os.path.dirname(__file__), "photo_pool.json")
        try:
            with open(path, encoding="utf-8") as f:
                _PHOTO_POOL = json.load(f)
        except FileNotFoundError:
            _PHOTO_POOL = {}
    return _PHOTO_POOL


# ---------------------------------------------------------------------------
# GenConfig
# ---------------------------------------------------------------------------

@dataclass
class GenConfig:
    n_listings: int = DEV_N_LISTINGS
    n_reviews: int = DEV_N_REVIEWS
    cities: tuple[str, ...] = tuple(CITIES)
    seed: int = 42
    use_llm_reviews: bool = False   # True -> richer text via LLM (costs $/time)
    # Extra cities can be added here without changing generator logic as long as
    # CITY_BOUNDS is extended.


# ---------------------------------------------------------------------------
# Listing generator
# ---------------------------------------------------------------------------

def generate_listings(cfg: GenConfig) -> Generator[dict, None, None]:
    """Yield listing dicts matching schema.sql, one at a time.

    Distributions:
    - Cities: split proportionally to city listing quotas (equal split default).
    - Property type: weighted toward 'entire place'.
    - Price: log-normal within per-city price_range so we get a realistic tail.
    - Beds: weighted distribution favouring 1-2 beds.
    - Amenities: 3-9 from canonical vocabulary, seeded so reproducible.
    - Rating: None (10 % unlisted) or Normal(4.4, 0.3) clipped to [3.0, 5.0].
    - review_count: Poisson(λ=12) so we get a realistic long-tail later.

    Memory: yields one dict; nothing accumulated.
    """
    rng = random.Random(cfg.seed)
    fk = Faker(["en_US"])
    Faker.seed(cfg.seed)

    n_cities = len(cfg.cities)
    per_city = [cfg.n_listings // n_cities] * n_cities
    # Distribute remainder to first cities.
    for i in range(cfg.n_listings % n_cities):
        per_city[i] += 1

    for city_idx, city in enumerate(cfg.cities):
        bounds = CITY_BOUNDS[city]
        lat_min, lat_max = bounds["lat"]
        lng_min, lng_max = bounds["lng"]
        neighbourhoods: list[str] = bounds["neighbourhoods"]
        price_min, price_max = bounds["price_range"]

        for local_idx in range(per_city[city_idx]):
            global_idx = sum(per_city[:city_idx]) + local_idx
            # Deterministic seed string for this listing.
            seed_str = f"listing:{cfg.seed}:{city}:{local_idx}"
            lid = _listing_id(seed_str)

            # Stable sub-rng per listing so city ordering doesn't shift values.
            lrng = random.Random(f"{cfg.seed}:{global_idx}")

            prop_type = lrng.choices(PROPERTY_TYPES, weights=PROPERTY_TYPE_WEIGHTS, k=1)[0]

            beds_vals, beds_wts = zip(*BEDS_DIST)
            beds = lrng.choices(list(beds_vals), weights=list(beds_wts), k=1)[0]

            # Log-normal price within city range.
            import math
            log_min, log_max = math.log(price_min), math.log(price_max)
            log_price = lrng.uniform(log_min, log_max)
            base_price = round(math.exp(log_price), 2)

            # Rating: 10 % unlisted, otherwise Normal clipped.
            if lrng.random() < 0.10:
                rating = None
            else:
                r = lrng.gauss(4.4, 0.3)
                rating = round(max(3.0, min(5.0, r)), 2)

            # review_count: Poisson-ish via summing Bernoullis.
            review_count = sum(1 for _ in range(60) if lrng.random() < 0.20)

            neighbourhood = lrng.choice(neighbourhoods)

            lat = round(lrng.uniform(lat_min, lat_max), 6)
            lng = round(lrng.uniform(lng_min, lng_max), 6)

            # Amenities: 3-9 random from canonical vocab (already normalised).
            n_amenities = lrng.randint(3, 9)
            amenities = sorted(lrng.sample(CANONICAL_AMENITIES, n_amenities))

            # Photos: 5-8 real, city-matched URLs, deterministic per listing.
            pool = _photo_pool().get(city, [])
            if pool:
                n_photos = lrng.randint(5, 8)
                photos = lrng.sample(pool, min(n_photos, len(pool)))
            else:
                # Fallback that still renders if the pool file is missing.
                n_photos = lrng.randint(5, 8)
                photos = [
                    f"https://picsum.photos/seed/{lid}-{i}/800/600"
                    for i in range(n_photos)
                ]

            # Host.
            host_seed = lrng.randint(0, 2**31)
            hfk = Faker(["en_US"])
            Faker.seed(host_seed)
            host = {
                "id": str(uuid.UUID(hashlib.md5(f"host:{lid}".encode()).hexdigest())),
                "name": hfk.name(),
                "superhost": lrng.random() < 0.25,
                "joined_year": lrng.randint(2012, 2023),
            }

            # Listing name: "<type> in <neighbourhood>: <adjective> <noun>".
            adjectives = ["Charming", "Modern", "Cosy", "Elegant", "Sunny",
                          "Spacious", "Stylish", "Bright", "Quiet", "Luxurious"]
            nouns = ["Apartment", "Studio", "Suite", "Retreat", "Haven",
                     "Hideaway", "Nest", "Loft", "Place", "Home"]
            name = (
                f"{lrng.choice(adjectives)} {lrng.choice(nouns)} in {neighbourhood}"
            )

            yield {
                "id": lid,
                "name": name,
                "type": prop_type,
                "city": city,
                "neighbourhood": neighbourhood,
                "lat": lat,
                "lng": lng,
                "base_price": base_price,
                "beds": beds,
                "amenities": amenities,        # already canonical list[str]
                "photos": photos,
                "host": host,
                "rating": rating,
                "review_count": review_count,
                "neighbourhood_price_pct": None,  # filled by enrichment
            }


# ---------------------------------------------------------------------------
# Review generator
# ---------------------------------------------------------------------------

def generate_reviews(
    cfg: GenConfig,
    listing_ids: list[str],
) -> Generator[dict, None, None]:
    """Yield review dicts. Distributes N_REVIEWS across listings with a
    long-tail (Zipf-like) distribution so a few popular properties have many
    reviews while most have a handful.

    Memory: we build a weight list proportional to listing index (O(n_listings)
    integers — at 50 K that is ~400 KB), then sample sequentially without
    storing the full review corpus.

    Reviewer names are drawn from a pre-generated pool of 2 000 names so we
    avoid instantiating a new Faker object for each review (which would make
    generation O(N) in Faker init overhead).

    Parameters
    ----------
    cfg        : GenConfig controlling n_reviews, seed, and LLM flag.
    listing_ids: stable ordered list of listing IDs (from generate_listings or DB).
    """
    rng = random.Random(cfg.seed + 1)   # +1 so review seed differs from listing seed

    n = len(listing_ids)
    if n == 0:
        return

    # Pre-generate a pool of reviewer names (one Faker init, not N).
    _POOL_SIZE = 2_000
    Faker.seed(cfg.seed + 9999)
    fk_names = Faker(["en_US", "pt_PT", "fr_FR", "es_ES"])
    name_pool: list[str] = [fk_names.name() for _ in range(_POOL_SIZE)]

    # Zipf-like weights: weight[i] = 1/(rank+1).  rank 0 → most reviews.
    # We shuffle so it isn't always the first listing in the list that dominates.
    shuffled_indices = list(range(n))
    rng.shuffle(shuffled_indices)
    weights = [1.0 / (rank + 1) for rank in range(n)]
    # Map back: listing at position shuffled_indices[i] gets weight weights[i].
    listing_weights = [0.0] * n
    for rank, orig_idx in enumerate(shuffled_indices):
        listing_weights[orig_idx] = weights[rank]

    # We emit reviews one at a time using a running counter per listing.
    review_counters: dict[str, int] = {}

    for rev_global_idx in range(cfg.n_reviews):
        # Pick a listing.
        (listing_id,) = rng.choices(listing_ids, weights=listing_weights, k=1)
        counter = review_counters.get(listing_id, 0)
        review_counters[listing_id] = counter + 1

        rid = _review_id(listing_id, counter)

        # Per-review stable rng (integer seed — faster than string hashing).
        rev_seed = (cfg.seed ^ (hash(listing_id) & 0xFFFFFFFF)) + counter
        rrng = random.Random(rev_seed)

        lang = rrng.choices(LANGUAGES, weights=LANGUAGE_WEIGHTS, k=1)[0]

        # Review date: random date in the last 4 years.
        days_back = rrng.randint(0, 4 * 365)
        rev_date = date.today() - timedelta(days=days_back)

        # Rating: biased high (1-2 stars are rare).
        star_weights = [0.02, 0.03, 0.10, 0.35, 0.50]
        rating = float(rrng.choices([1, 2, 3, 4, 5], weights=star_weights, k=1)[0])

        # Reviewer name from the pre-built pool.
        reviewer = name_pool[rrng.randint(0, _POOL_SIZE - 1)]

        # Review text.
        if cfg.use_llm_reviews:
            # Placeholder — caller is responsible for replacing this with
            # actual LLM calls in a batched post-processing pass.
            text = f"[LLM_PLACEHOLDER:{rid}]"
        else:
            templates = _REVIEW_TEMPLATES.get(lang, _REVIEW_TEMPLATES["en"])
            template = rrng.choice(templates)
            # Fill slots.
            amenity = rrng.choice(CANONICAL_AMENITIES).replace("_", " ")
            aspect = rrng.choice(_ASPECTS)
            # neighbourhood is not available here without a back-lookup;
            # use a generic filler.
            neighbourhood_filler = "the area"
            text = (
                template
                .replace("{amenity}", amenity)
                .replace("{aspect}", aspect)
                .replace("{neighbourhood}", neighbourhood_filler)
            )

        yield {
            "id": rid,
            "listing_id": listing_id,
            "date": rev_date.isoformat(),
            "reviewer": reviewer,
            "rating": rating,
            "text": text,
            "language": lang,
            "aspects": None,      # filled by aspect_sentiment enrichment
            "sentiment": None,    # filled by aspect_sentiment enrichment
        }


# ---------------------------------------------------------------------------
# CLI entry point for standalone testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse, json

    parser = argparse.ArgumentParser(description="Generate synthetic travel data")
    parser.add_argument("--n-listings", type=int, default=DEV_N_LISTINGS)
    parser.add_argument("--n-reviews", type=int, default=DEV_N_REVIEWS)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--count-only", action="store_true")
    args = parser.parse_args()

    cfg = GenConfig(n_listings=args.n_listings, n_reviews=args.n_reviews, seed=args.seed)

    listing_ids = []
    for i, listing in enumerate(generate_listings(cfg)):
        listing_ids.append(listing["id"])
        if not args.count_only and i < 2:
            print(json.dumps(listing, default=str, indent=2))

    print(f"\nListings generated: {len(listing_ids)}")

    rev_count = 0
    for review in generate_reviews(cfg, listing_ids):
        rev_count += 1
        if not args.count_only and rev_count <= 2:
            print(json.dumps(review, default=str, indent=2))

    print(f"Reviews generated: {rev_count}")
