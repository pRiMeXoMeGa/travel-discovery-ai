"""Ingestion enrichments (brief §2.1 — at least two non-trivial ones).

Chosen enrichments (each unlocks UI/agent requirements):
  1. Aspect-level sentiment per review  -> review topic filtering + aspect scores
  2. Per-property review summary         -> "AI summary at top" + compare verdict
  3. Neighbourhood price percentile      -> "is this expensive for the area"
  4. Amenity normalization               -> consistent amenity filters
(Embeddings are handled in ingest.py since the Retrieval agent needs them.)
"""


async def aspect_sentiment(review_text: str) -> dict:
    """Return per-aspect sentiment: {cleanliness, location, value, staff, noise}.

    TODO: LLM structured-output call (batch for throughput/cost). Score each
    aspect in [-1, 1] or null if not mentioned.
    """
    raise NotImplementedError("TODO: aspect sentiment")


async def summarize_property(listing_id: str, reviews: list[str]) -> dict:
    """Return {summary, aspect_avg} for a property.

    TODO: LLM synthesis over (a sample of) the property's reviews. Precompute
    for all 50K (slow on free rate limits) OR top-N + on-demand cache the rest.
    """
    raise NotImplementedError("TODO: property summary")


def neighbourhood_price_percentile(conn) -> None:
    """Compute each listing's price percentile within its neighbourhood.

    TODO: single SQL UPDATE using percent_rank() OVER (PARTITION BY neighbourhood
    ORDER BY base_price). Cheap, no LLM.
    """
    raise NotImplementedError("TODO: price percentile")


def normalize_amenities(raw: list[str]) -> list[str]:
    """Map free-form amenity strings to a canonical vocabulary."""
    raise NotImplementedError("TODO: amenity normalization")
