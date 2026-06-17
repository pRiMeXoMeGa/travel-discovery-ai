"""Query-time embeddings via fastembed (ONNX bge-small, 384-dim).

fastembed is intentionally chosen over sentence-transformers/torch: it is light
enough to run inside the Render free 512MB instance. The same model is used at
ingest so query and corpus vectors live in the same space.
"""
import asyncio
from functools import lru_cache

from fastembed import TextEmbedding

from .config import settings


@lru_cache(maxsize=1)
def _model() -> TextEmbedding:
    return TextEmbedding(model_name=settings.embedding_model)


def _embed_sync(texts: list[str]) -> list[list[float]]:
    return [vec.tolist() for vec in _model().embed(texts)]


async def embed_texts(texts: list[str]) -> list[list[float]]:
    # Run the (CPU-bound, sync) model off the event loop.
    return await asyncio.to_thread(_embed_sync, texts)


async def embed_query(text: str) -> list[float]:
    return (await embed_texts([text]))[0]
