"""mem0 embedder backed by the app's existing fastembed model.

Why this exists
---------------
mem0 defaults to an API embedder, and its `huggingface` provider pulls
sentence-transformers -> torch, which does not fit Render's free 512MB instance.
This repo already embeds with fastembed (ONNX bge-small, 384-dim) locally; memory
vectors must live in that same space.

Two decisions taken from app/embeddings.py rather than assumed — both re-verified
against the current file:

1. It imports `_model()` directly. That function is `@lru_cache(maxsize=1)`, so
   this shares the one process-wide TextEmbedding — no second copy of the ONNX
   weights, no extra MB. (Confirmed: app/embeddings.py defines
   `@lru_cache(maxsize=1) def _model()`.)

2. It calls plain `.embed()` for every memory_action. `embed_query()` in
   app/embeddings.py also uses `.embed()`, never `query_embed()`, so the corpus
   carries no bge query prefix. Introducing one here would put memory queries in
   a subtly different space and quietly degrade recall — nothing would raise.
   If app/embeddings.py ever switches to `query_embed()`, this must switch too.
   (Confirmed: `_embed_sync` calls `_model().embed(texts)`; `embed_query` is
   `(await embed_texts([text]))[0]`.)

Note the asymmetry this creates: app/embeddings.py runs the model through
`asyncio.to_thread`, but mem0's EmbeddingBase interface is synchronous, so this
class cannot. That is why every call site must go through app/memory/store.py,
which re-establishes the to_thread boundary.

Net cost of memory retrieval: zero API calls.

Wiring
------
    from mem0.utils.factory import EmbedderFactory
    EmbedderFactory.provider_to_class["fastembed"] = (
        "app.memory.fastembed_embedder.FastEmbedEmbedder"
    )

("fastembed" is not a real mem0 provider name — this registers one.)

If that registry attribute has moved in your installed mem0 version, skip the
factory and assign after construction instead:

    memory = Memory.from_config(config)
    memory.embedding_model = FastEmbedEmbedder(None)
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from mem0.embeddings.base import EmbeddingBase

from ..embeddings import _model  # lru_cache(maxsize=1) — the shared instance

EMBEDDING_DIMS = 384


class FastEmbedEmbedder(EmbeddingBase):
    """Local, free, 384-dim embedder for mem0."""

    def __init__(self, config: Any = None) -> None:
        super().__init__(config)
        self.dims = getattr(config, "embedding_dims", None) or EMBEDDING_DIMS

    def embed(
        self, text: str, memory_action: Optional[str] = None
    ) -> Sequence[float]:
        """Return one 384-dim vector.

        NOTE: synchronous by mem0's interface contract, and CPU-bound. Never call
        it from the event loop — go through app/memory/store.py, which wraps every
        mem0 entry point in asyncio.to_thread, matching embed_texts().
        """
        if not text or not text.strip():
            return [0.0] * self.dims

        # memory_action is ignored on purpose — see module docstring, point 2.
        vectors = list(_model().embed([text]))
        if not vectors:
            raise ValueError("fastembed returned no vectors")

        vector = vectors[0].tolist()
        if len(vector) != self.dims:
            raise ValueError(
                f"expected {self.dims}-dim vector, got {len(vector)}. The Qdrant "
                "collection and embedding_model_dims must agree, or upsert fails "
                "with an opaque shape error."
            )
        return vector


def assert_local_and_gemini(memory: Any) -> None:
    """Fail loudly at startup if mem0 silently fell back to a remote provider.

    mem0 does this without warning when a provider key is omitted or misspelled.
    Call from the FastAPI lifespan, next to the existing pool/Qdrant setup.
    """
    embedder = getattr(memory, "embedding_model", None)
    if not isinstance(embedder, FastEmbedEmbedder):
        raise RuntimeError(
            f"mem0 embedder is {type(embedder).__name__}, expected "
            "FastEmbedEmbedder — it has fallen back to a remote provider."
        )

    llm_impl = getattr(memory, "llm", None)
    if "gemini" not in type(llm_impl).__name__.lower():
        raise RuntimeError(
            f"mem0 LLM is {type(llm_impl).__name__}, expected a Gemini provider."
        )
