"""Tests for the WS3 LangGraph planner — routing logic and the Redis checkpointer.

`langgraph` is a heavy dependency and is deliberately NOT in
`requirements-dev.txt`, so the graph tests importorskip it and do not run in CI
(same arrangement as `test_mcp_server.py` with fastmcp). The pure routing
predicates are tested unconditionally, since those are where the interesting
decisions live and they need nothing installed.

Zero network, zero LLM quota: the checkpointer talks to a fake in-memory Redis.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

langgraph = pytest.importorskip("langgraph", reason="langgraph is not a dev dependency")

from app.planner import graph as pg  # noqa: E402


# ── the replan cycle predicate ───────────────────────────────────────────────

def test_empty_plan_needs_a_replan():
    """The case the cycle most needs to catch.

    An empty plan carries within_budget=None (see itinerary.py), NOT False, so
    a predicate testing only `is False` would skip it — which is exactly the
    bug that made the cycle never fire on an impossible budget.
    """
    assert pg._needs_replan({"stays": [], "within_budget": None}) is True
    assert pg._needs_replan({}) is True


def test_over_budget_plan_needs_a_replan():
    assert pg._needs_replan({"stays": [{"segment": 1}], "within_budget": False}) is True


def test_good_plan_does_not_replan():
    assert pg._needs_replan({"stays": [{"segment": 1}], "within_budget": True}) is False
    # No budget given at all — nothing to be over.
    assert pg._needs_replan({"stays": [{"segment": 1}], "within_budget": None}) is False


def test_cycle_is_bounded():
    """An unbounded replan loop on an impossible budget bills LLM calls forever."""
    bad = {"plan": {"stays": [], "within_budget": None}}
    assert pg._after_budget({**bad, "replans": 0}) == "plan"
    assert pg._after_budget({**bad, "replans": pg.MAX_REPLANS}) == "plan"
    assert pg._after_budget({**bad, "replans": pg.MAX_REPLANS + 1}) == "review"


def test_review_routes_on_the_human_decision():
    good = {"plan": {"stays": [{"s": 1}], "within_budget": True}}
    assert pg._after_review({**good, "decision": "approve", "replans": 0}) == "finalize"
    assert pg._after_review({**good, "decision": "adjust", "replans": 0}) == "plan"
    # An adjust loop is bounded too.
    assert pg._after_review(
        {**good, "decision": "adjust", "replans": pg.MAX_REPLANS + 1}
    ) == "finalize"


# ── error guard ──────────────────────────────────────────────────────────────

async def test_node_guard_degrades_instead_of_killing_the_stream():
    """LangGraph propagates a node exception out of astream, which on an SSE
    endpoint truncates the response with no explanation."""
    async def boom(_state):
        raise RuntimeError("node exploded")

    out = await pg._guard(boom, "plan")({})
    assert out["errors"] and "RuntimeError" in out["errors"][0]
    assert out["events"][0]["status"] == "error"


# ── checkpointer ─────────────────────────────────────────────────────────────

class _FakeRedis:
    """Enough of redis.asyncio for the checkpointer: get/set/zadd/zrevrange."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.z: dict[str, dict[str, float]] = {}

    async def get(self, k):
        return self.kv.get(k)

    async def set(self, k, v, ex=None):
        self.kv[k] = v

    async def zadd(self, k, mapping):
        self.z.setdefault(k, {}).update(mapping)

    async def zcard(self, k):
        return len(self.z.get(k, {}))

    async def zrevrange(self, k, start, end):
        items = sorted(self.z.get(k, {}).items(), key=lambda kv: kv[1], reverse=True)
        ids = [i for i, _ in items]
        return ids[start:] if end == -1 else ids[start:end + 1]

    async def expire(self, k, ttl):
        return True


@pytest.fixture
def saver(monkeypatch):
    from app.planner import checkpointer as cp

    fake = _FakeRedis()
    monkeypatch.setattr(cp, "get_redis", lambda: fake)
    return cp.RedisCheckpointSaver()


async def test_checkpoint_round_trips(saver):
    """Resume across a process restart depends entirely on this."""
    cfg = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    checkpoint = {"id": "c1", "channel_values": {"query": "Lisbon"}, "v": 1}
    out = await saver.aput(cfg, checkpoint, {"source": "loop"}, {})

    assert out["configurable"]["checkpoint_id"] == "c1"

    tup = await saver.aget_tuple({"configurable": {"thread_id": "t1"}})
    assert tup is not None
    assert tup.checkpoint["id"] == "c1"
    assert tup.checkpoint["channel_values"]["query"] == "Lisbon"


async def test_latest_checkpoint_wins_when_no_id_given(saver):
    cfg = {"configurable": {"thread_id": "t2", "checkpoint_ns": ""}}
    await saver.aput(cfg, {"id": "c1", "v": 1}, {}, {})
    await saver.aput(cfg, {"id": "c2", "v": 1}, {}, {})

    tup = await saver.aget_tuple({"configurable": {"thread_id": "t2"}})
    assert tup.checkpoint["id"] == "c2"


async def test_unknown_thread_returns_none_rather_than_raising(saver):
    assert await saver.aget_tuple({"configurable": {"thread_id": "nope"}}) is None
    assert await saver.aget_tuple({"configurable": {}}) is None


async def test_read_failure_degrades_to_no_checkpoint(saver, monkeypatch):
    """A checkpoint we cannot read is indistinguishable from no checkpoint;
    failing the request is worse than starting the plan over."""
    from app.planner import checkpointer as cp

    class _Broken(_FakeRedis):
        async def zrevrange(self, *a, **k):
            raise RuntimeError("redis down")

    monkeypatch.setattr(cp, "get_redis", lambda: _Broken())
    assert await saver.aget_tuple({"configurable": {"thread_id": "t1"}}) is None


async def test_write_failure_raises_rather_than_silently_losing_the_turn(saver, monkeypatch):
    """The opposite call to reads: a dropped write means resume loses state,
    so the caller must find out now."""
    from app.planner import checkpointer as cp

    class _Broken(_FakeRedis):
        async def set(self, *a, **k):
            raise RuntimeError("redis down")

    monkeypatch.setattr(cp, "get_redis", lambda: _Broken())
    with pytest.raises(RuntimeError):
        await saver.aput(
            {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}},
            {"id": "c9", "v": 1}, {}, {},
        )


async def test_pending_writes_round_trip(saver):
    cfg = {"configurable": {"thread_id": "t3", "checkpoint_ns": "", "checkpoint_id": "c1"}}
    await saver.aput({"configurable": {"thread_id": "t3", "checkpoint_ns": ""}},
                     {"id": "c1", "v": 1}, {}, {})
    await saver.aput_writes(cfg, [("channel_a", {"x": 1})], "task-1")

    tup = await saver.aget_tuple(cfg)
    assert tup.pending_writes == [("task-1", "channel_a", {"x": 1})]
