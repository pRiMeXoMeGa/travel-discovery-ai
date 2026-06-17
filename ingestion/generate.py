"""Synthetic data generation (brief §2.1, Option B).

Re-runnable. Produces >=50K listings + >=200K reviews across >=2 cities with the
required schema. Faker handles structured fields; an LLM can optionally generate
realistic review text (document any cost in the README).

Run standalone to emit data, or import from ingest.py.
"""
from dataclasses import dataclass

# Target scale (brief floor). Spread across >=2 cities.
N_LISTINGS = 50_000
N_REVIEWS = 200_000
CITIES = ["Lisbon", "Dubai"]  # add more as needed

PROPERTY_TYPES = ["entire place", "private room", "hotel", "shared room"]
AMENITIES = ["wifi", "pool", "kitchen", "parking", "balcony", "ac", "gym", "washer", "pets"]
LANGUAGES = ["en", "pt", "ar", "fr", "es"]


@dataclass
class GenConfig:
    n_listings: int = N_LISTINGS
    n_reviews: int = N_REVIEWS
    cities: tuple[str, ...] = tuple(CITIES)
    use_llm_reviews: bool = False  # True -> richer text via LLM (costs money/time)


def generate_listings(cfg: GenConfig):
    """Yield listing dicts matching schema.sql.

    TODO: use Faker + per-city lat/lng bounding boxes + realistic price/beds/
    amenity distributions. Normalize amenities to the AMENITIES vocabulary
    (enrichment: amenity normalization).
    """
    raise NotImplementedError("TODO: generate_listings")


def generate_reviews(cfg: GenConfig, listing_ids: list[str]):
    """Yield review dicts. Distribute ~N_REVIEWS across listings (long-tail).

    TODO: Faker for metadata; review text either templated or LLM-generated
    (cfg.use_llm_reviews). Vary language per LANGUAGES.
    """
    raise NotImplementedError("TODO: generate_reviews")


if __name__ == "__main__":
    # TODO: write to CSV/parquet under ./data, or stream straight into ingest.py.
    print("TODO: generate synthetic dataset")
