"""Review-synthesis service: wraps `agents/review_intel` with an explicit
abstention contract.

`review_intel.synthesize()` returns `tuple[str, list[Citation]]`. When there
is no evidence — no listing specified, or no reviews retrieved for the given
listing(s) — it still returns a plain sentence and an EMPTY citation list;
nothing on the wire distinguishes "no evidence" from "there are citations".
WS2's `synthesize_reviews` MCP tool needs to say which case it's in (its
docstring promises `abstained: true` when no reviews match, so a calling
agent can tell "no evidence" from "failure" rather than treating a hedging
sentence as a grounded claim). This module adds that flag deterministically
from the citation list — never by parsing the generated prose.
"""
import logging

from ..agents import review_intel
from ..observability import AgentStep

logger = logging.getLogger(__name__)

# Exact prefixes of review_intel.synthesize()'s two no-evidence sentences —
# used only to label *why* citations came back empty; the abstention
# decision itself never depends on this (it depends only on `citations`
# being empty, see below).
_NO_LISTING_PREFIX = "No property was specified"
_NO_REVIEWS_PREFIX = "No guest reviews are available"


async def synthesize_reviews(
    listing_ids: list[str], focus: str | None = None, step: AgentStep | None = None
) -> dict:
    """Return a grounded review synthesis with an explicit abstention flag.

    Shape: {"text": str, "citations": list[Citation], "abstained": bool,
    "reason": str}.

    `abstained` is True exactly when `review_intel.synthesize` returned zero
    citations. That happens on exactly two paths in `review_intel.py`, both
    early returns before any LLM call: no `listing_ids` given, or no review
    rows retrieved for the given listing(s). (When snippets exist, at least
    one `[r#]` label is always available to cite — `synthesize` falls back
    to citing every retrieved snippet if the model's prose cites none — so a
    non-empty snippet set can never produce an empty citation list.)
    `reason` distinguishes the two no-evidence paths for a caller that wants
    to say something more specific than "no evidence"; it is "" when
    `abstained` is False.
    """
    text, citations = await review_intel.synthesize(listing_ids, focus=focus, step=step)

    if citations:
        return {"text": text, "citations": citations, "abstained": False, "reason": ""}

    if text.startswith(_NO_LISTING_PREFIX):
        reason = "no_listing_specified"
    elif text.startswith(_NO_REVIEWS_PREFIX):
        reason = "no_reviews"
    else:
        # Defensive: some future no-citation path we haven't special-cased.
        # Still honestly "no evidence a caller can verify".
        reason = "no_evidence"
    return {"text": text, "citations": [], "abstained": True, "reason": reason}
