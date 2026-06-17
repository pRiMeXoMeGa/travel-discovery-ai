"""Redis cache for repeated retrievals and review syntheses.

Travel queries cluster heavily, so caching matters (brief §2.4). Use stable,
normalized cache keys (e.g. a hash of the structured query) so semantically
identical requests hit the cache.
"""
import json
from typing import Any

import redis.asyncio as redis

from .config import settings

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def cache_get(key: str) -> Any | None:
    raw = await get_redis().get(key)
    return json.loads(raw) if raw else None


async def cache_set(key: str, value: Any, ttl: int | None = None) -> None:
    await get_redis().set(
        key, json.dumps(value), ex=ttl or settings.cache_ttl_seconds
    )


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
