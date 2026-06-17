"""Retrieval agent: semantic + filtered + geospatial search.

Combines a Qdrant vector search (semantic) with Postgres structured filters
(price, type, amenities, rating) and a geospatial distance signal, then returns
a ranked candidate set with a per-result rationale (brief §2.3).
"""
from ..schemas import ListingCard, StructuredQuery


async def retrieve(sq: StructuredQuery, limit: int = 20) -> list[tuple[ListingCard, str]]:
    """Return ranked (listing, rationale) candidates.

    TODO:
      1. embed the query intent -> Qdrant search over `listings` (semantic).
      2. apply hard constraints as Postgres filters (price, type, amenities).
      3. apply geospatial filter/sort (distance to reference point / POI).
      4. fuse + rank; produce a short rationale per result citing why it matched.
    Keep candidate sets bounded (memory awareness, brief §3).
    """
    raise NotImplementedError("TODO: retrieval")
