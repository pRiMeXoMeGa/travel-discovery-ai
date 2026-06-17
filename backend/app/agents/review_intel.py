"""Review intelligence agent: synthesize review insights with citations.

For any property or candidate set, summarize what guests consistently praise /
complain about, and surface review consistency — every claim cites an actual
review (brief §2.3, grounding/§3). Uses precomputed per-property summaries +
aspect sentiment where available; falls back to on-demand synthesis (cached).
"""
from ..schemas import Citation


async def synthesize(listing_ids: list[str], focus: str | None = None) -> tuple[str, list[Citation]]:
    """Return (synthesis_text, citations).

    TODO:
      1. pull precomputed summary + aspect sentiment from Postgres if present.
      2. otherwise embed `focus` and retrieve top relevant reviews from Qdrant.
      3. LLM-synthesize a grounded summary; REFUSE/hedge if no supporting
         reviews exist (no fabrication).
      4. attach Citation objects pointing at the actual review rows.
      5. cache the result (Redis).
    """
    raise NotImplementedError("TODO: review synthesis")
