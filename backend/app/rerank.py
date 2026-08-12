"""Cross-encoder reranking (WS4) — lazy, optional, and OFF by default.

Retrieval gives you a bi-encoder ordering: query and document are embedded
independently, so the model never sees them together. A cross-encoder scores the
PAIR, which is strictly more informative and reliably reorders the head of the
list. Retrieve wide (50), rerank narrow (10).

WHY IT IS OFF BY DEFAULT — measured, not assumed
------------------------------------------------
On the 512 MB Render free instance this app already peaks at ~479 MB once the
concierge (bge-small, +212 MB), memory and the LangGraph planner (+70 MB) have
all been exercised. `TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2')` — the
SMALLEST supported reranker at 80 MB on disk — costs a further **+156 MB
resident** and 20.8s to construct. That lands around 635 MB: the instance would
be OOM-killed, and the first request to touch it would pay a 20s stall.

Latency is a second, independent problem: reranking 50 documents took **1064 ms**
on this single-vCPU box. That is why it never goes on the concierge's streaming
path even when enabled — it would add a second of dead air before the first
token.

So this ships behind `RERANK_ENABLED`, default false. `scripts/rerank_eval.py`
measures the quality delta offline, without ever loading the model into the API
process, so the trade-off is documented with numbers rather than asserted.

Enable it only on an instance with real headroom (>=1 GB).
"""
from __future__ import annotations

import logging
from typing import Sequence

from .config import settings

logger = logging.getLogger(__name__)

_encoder = None
_load_failed = False


def enabled() -> bool:
    return bool(getattr(settings, "rerank_enabled", False))


def _get_encoder():
    """Construct the cross-encoder once, on first use.

    Deliberately lazy rather than loaded at startup: when the flag is off the
    model must never be constructed, and even when it is on, paying 20s at boot
    would make the Render health check fail before the app ever serves.
    """
    global _encoder, _load_failed
    if _encoder is not None or _load_failed:
        return _encoder
    try:
        # Not exported from the fastembed top level — it lives here. The plan
        # recorded it as absent from 0.4.2, which cost a needless version-bump
        # scare: bumping fastembed risks changing bge-small's output and
        # invalidating all 50K corpus vectors.
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        _encoder = TextCrossEncoder(model_name=settings.rerank_model)
        logger.info("cross-encoder loaded: %s", settings.rerank_model)
    except Exception as exc:  # noqa: BLE001
        # Never fatal. Reranking is an ordering improvement; without it the
        # bi-encoder ordering is still a perfectly good answer.
        _load_failed = True
        logger.warning(
            "cross-encoder unavailable (%s: %s) — serving unreranked order",
            type(exc).__name__, exc,
        )
    return _encoder


def rerank_indices(query: str, documents: Sequence[str]) -> list[int] | None:
    """Return document indices in reranked order, or None if unavailable.

    Returning indices rather than reordered documents keeps this module free of
    any knowledge of ListingCard — the caller owns its own payload shape.

    None means "no opinion": the flag is off, the model failed to load, or
    scoring raised. Every caller must treat that as "keep the existing order",
    which is why this returns None rather than raising or silently returning the
    input order (the latter would hide a broken model as a no-op).
    """
    if not enabled() or not documents:
        return None
    encoder = _get_encoder()
    if encoder is None:
        return None
    try:
        scores = list(encoder.rerank(query, list(documents)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("rerank scoring failed: %s: %s", type(exc).__name__, exc)
        return None
    if len(scores) != len(documents):
        # A partial result would silently drop candidates.
        logger.warning(
            "rerank returned %d scores for %d documents — ignoring",
            len(scores), len(documents),
        )
        return None
    return sorted(range(len(documents)), key=lambda i: scores[i], reverse=True)


def listing_to_document(card, rationale: str = "") -> str:
    """Flatten a listing into the text the cross-encoder scores.

    Only fields the corpus actually guarantees: a None slipping in here becomes
    the literal string "None" in the scored text and quietly degrades ranking.
    """
    parts = [
        getattr(card, "name", "") or "",
        getattr(card, "neighbourhood", "") or "",
        getattr(card, "city", "") or "",
        getattr(card, "type", "") or "",
    ]
    amenities = getattr(card, "key_amenities", None) or []
    if amenities:
        parts.append(", ".join(amenities))
    if rationale:
        parts.append(rationale)
    return ". ".join(p for p in parts if p)
