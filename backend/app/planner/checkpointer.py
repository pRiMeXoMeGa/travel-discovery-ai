"""A LangGraph checkpointer backed by the app's existing Redis (WS3).

Why not `langgraph-checkpoint-postgres`: it pulls psycopg, and this app already
talks to Postgres through asyncpg. Two Postgres drivers in one 512 MB Render
instance is a real cost for no benefit — the state being persisted here is a
handful of small JSON blobs per thread, not relational data.

Why not the in-memory saver: the whole point of the HITL interrupt is that a
plan can be presented, the process can go away (Render free tier spins down
after 15 minutes idle), and the traveller can still resume. An in-memory
checkpointer makes interrupt/resume work in a demo and fail in production —
which is worse than not having it, because the failure only shows up after a
cold start.

Serialisation goes through LangGraph's own `JsonPlusSerializer` rather than
plain `json.dumps`: checkpoint values contain types (datetimes, pydantic
models) that json cannot round-trip, and a serializer mismatch surfaces much
later as a confusing decode error on resume rather than an error on write.

Every key is namespaced and given a TTL. A checkpoint is not durable state that
must live forever — it is a resumable conversation, and an abandoned one should
expire rather than accumulate in a free-tier Redis with a hard key limit.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Optional, Sequence

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from ..cache import get_redis

logger = logging.getLogger(__name__)

# 7 days. Long enough that "I'll finish planning tomorrow" works, short enough
# that abandoned threads do not accumulate forever on a free-tier Redis.
CHECKPOINT_TTL_SECONDS = 7 * 24 * 3600

_PREFIX = "planner:ckpt"


def _thread_key(thread_id: str, checkpoint_ns: str = "") -> str:
    """Sorted-set key holding every checkpoint id for one thread."""
    return f"{_PREFIX}:{thread_id}:{checkpoint_ns}:index"


def _blob_key(thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
    return f"{_PREFIX}:{thread_id}:{checkpoint_ns}:c:{checkpoint_id}"


def _writes_key(thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
    return f"{_PREFIX}:{thread_id}:{checkpoint_ns}:w:{checkpoint_id}"


class RedisCheckpointSaver(BaseCheckpointSaver):
    """Minimal async checkpointer over the existing Redis client.

    Implements the four methods LangGraph actually calls on the async path:
    `aget_tuple`, `alist`, `aput`, `aput_writes`. The synchronous variants are
    deliberately not implemented — this app is async throughout, and a silently
    blocking sync path inside an SSE generator is exactly the kind of bug that
    only shows up under concurrency.
    """

    def __init__(self, ttl_seconds: int = CHECKPOINT_TTL_SECONDS) -> None:
        super().__init__(serde=JsonPlusSerializer())
        self.ttl = ttl_seconds

    # ── helpers ──────────────────────────────────────────────────────────────
    def _dumps(self, obj: Any) -> str:
        type_, payload = self.serde.dumps_typed(obj)
        # Store the type tag with the payload; dumps_typed/loads_typed are a
        # pair and dropping the tag makes the value undecodable.
        return f"{type_}\x00{payload.decode('latin-1')}"

    def _loads(self, raw: str) -> Any:
        type_, _, payload = raw.partition("\x00")
        return self.serde.loads_typed((type_, payload.encode("latin-1")))

    # ── read ─────────────────────────────────────────────────────────────────
    async def aget_tuple(self, config: dict) -> Optional[CheckpointTuple]:
        cfg = config.get("configurable", {})
        thread_id = cfg.get("thread_id")
        if not thread_id:
            return None
        ns = cfg.get("checkpoint_ns", "") or ""
        checkpoint_id = cfg.get("checkpoint_id")

        r = get_redis()
        try:
            if checkpoint_id is None:
                # Latest = highest-scored member of the index.
                latest = await r.zrevrange(_thread_key(thread_id, ns), 0, 0)
                if not latest:
                    return None
                checkpoint_id = latest[0]

            raw = await r.get(_blob_key(thread_id, ns, checkpoint_id))
            if raw is None:
                return None
            record = self._loads(raw)
        except Exception as exc:  # noqa: BLE001
            # A checkpoint we cannot read is indistinguishable from no
            # checkpoint, and failing the request is worse than starting over.
            logger.warning(
                "checkpoint read failed (thread=%s): %s: %s",
                thread_id, type(exc).__name__, exc,
            )
            return None

        pending = await self._read_writes(thread_id, ns, checkpoint_id)
        parent_id = record.get("parent_checkpoint_id")
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint=record["checkpoint"],
            metadata=record["metadata"],
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": ns,
                        "checkpoint_id": parent_id,
                    }
                }
                if parent_id
                else None
            ),
            pending_writes=pending,
        )

    async def _read_writes(
        self, thread_id: str, ns: str, checkpoint_id: str
    ) -> list[tuple[str, str, Any]]:
        try:
            raw = await get_redis().get(_writes_key(thread_id, ns, checkpoint_id))
            if raw is None:
                return []
            return [tuple(w) for w in self._loads(raw)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkpoint writes read failed: %s", exc)
            return []

    async def alist(
        self,
        config: Optional[dict],
        *,
        filter: Optional[dict] = None,  # noqa: A002 - LangGraph's parameter name
        before: Optional[dict] = None,
        limit: Optional[int] = None,
    ) -> AsyncIterator[CheckpointTuple]:
        cfg = (config or {}).get("configurable", {})
        thread_id = cfg.get("thread_id")
        if not thread_id:
            return
        ns = cfg.get("checkpoint_ns", "") or ""

        try:
            ids = await get_redis().zrevrange(_thread_key(thread_id, ns), 0, -1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkpoint list failed: %s", exc)
            return

        before_id = (before or {}).get("configurable", {}).get("checkpoint_id")
        seen = 0
        for cid in ids:
            if before_id and cid >= before_id:
                continue
            tup = await self.aget_tuple(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": ns,
                        "checkpoint_id": cid,
                    }
                }
            )
            if tup is None:
                continue
            yield tup
            seen += 1
            if limit is not None and seen >= limit:
                return

    # ── write ────────────────────────────────────────────────────────────────
    async def aput(
        self,
        config: dict,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> dict:
        cfg = config.get("configurable", {})
        thread_id = cfg["thread_id"]
        ns = cfg.get("checkpoint_ns", "") or ""
        checkpoint_id = checkpoint["id"]

        record = {
            "checkpoint": checkpoint,
            "metadata": metadata,
            "parent_checkpoint_id": cfg.get("checkpoint_id"),
        }
        r = get_redis()
        try:
            await r.set(
                _blob_key(thread_id, ns, checkpoint_id),
                self._dumps(record),
                ex=self.ttl,
            )
            index = _thread_key(thread_id, ns)
            # Score by checkpoint count so ordering is monotonic; LangGraph ids
            # are sortable strings, so the member itself is the tiebreak.
            await r.zadd(index, {checkpoint_id: await r.zcard(index) + 1})
            await r.expire(index, self.ttl)
        except Exception as exc:  # noqa: BLE001
            # Do NOT swallow this one: a failed write means resume will silently
            # lose the turn, which is worse than the caller seeing an error now.
            logger.error(
                "checkpoint write failed (thread=%s): %s: %s",
                thread_id, type(exc).__name__, exc,
            )
            raise

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: dict,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        cfg = config.get("configurable", {})
        thread_id = cfg["thread_id"]
        ns = cfg.get("checkpoint_ns", "") or ""
        checkpoint_id = cfg.get("checkpoint_id")
        if not checkpoint_id:
            return

        key = _writes_key(thread_id, ns, checkpoint_id)
        try:
            existing = await self._read_writes(thread_id, ns, checkpoint_id)
            merged = existing + [(task_id, channel, value) for channel, value in writes]
            await get_redis().set(key, self._dumps(merged), ex=self.ttl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkpoint writes failed: %s", exc)
