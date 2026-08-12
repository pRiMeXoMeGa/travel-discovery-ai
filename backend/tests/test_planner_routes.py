"""Tests for the WS3 planner HTTP surface.

The interesting logic here is not "does FastAPI route" — it is the event
de-duplication, which is easy to get wrong in a way that only shows up as a
client rendering the plan twice. That is tested against a fake graph and needs
no langgraph, so it runs in CI.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.routers import planner as pr  # noqa: E402


class _FakeGraph:
    """Mimics `astream(stream_mode='values')`: emits the WHOLE state each time,
    with `events` growing append-only — the behaviour `_drain` must cope with."""

    def __init__(self, event_batches):
        self._batches = event_batches

    def astream(self, payload, config, stream_mode=None):
        async def gen():
            acc = []
            for batch in self._batches:
                acc = acc + batch
                yield {"events": list(acc)}
        return gen()


async def _collect(graph, seen=0):
    return [ev async for ev, _count in pr._drain(graph, {}, {}, seen)]


# ── event de-duplication ─────────────────────────────────────────────────────

async def test_each_node_event_is_emitted_exactly_once():
    """`astream(values)` replays the full state after every node, and `events`
    is append-only — forwarding it naively sends event 1 once per node."""
    graph = _FakeGraph([
        [{"agent": "parse", "status": "done", "data": None}],
        [{"agent": "plan", "status": "done", "data": None}],
        [{"agent": "budget", "status": "done", "data": None}],
    ])
    out = await _collect(graph)
    assert [e["agent"] for e in out] == ["parse", "plan", "budget"]


async def test_seen_offset_skips_events_already_streamed():
    """Resume must not replay what the client saw before the interrupt."""
    graph = _FakeGraph([
        [{"agent": "parse", "status": "done", "data": None},
         {"agent": "plan", "status": "done", "data": None}],
        [{"agent": "review", "status": "done", "data": None}],
    ])
    out = await _collect(graph, seen=2)
    assert [e["agent"] for e in out] == ["review"]


async def test_multiple_events_from_one_node_all_surface():
    graph = _FakeGraph([
        [{"agent": "a", "status": "done", "data": None},
         {"agent": "b", "status": "error", "data": {"message": "x"}}],
    ])
    out = await _collect(graph)
    assert [(e["agent"], e["status"]) for e in out] == [("a", "done"), ("b", "error")]


async def test_drain_maps_node_events_onto_the_step_vocabulary():
    """The frontend's existing frame parser only understands step/data/done/error."""
    graph = _FakeGraph([[{"agent": "plan", "status": "done", "data": {"stays": 2}}]])
    out = await _collect(graph)
    assert out[0]["type"] == "step"
    assert out[0]["data"] == {"stays": 2}


# ── SSE framing ──────────────────────────────────────────────────────────────

def test_sse_frame_uses_the_event_type_as_the_event_name():
    frame = pr._sse({"type": "awaiting_input", "thread_id": "t1"})
    assert frame["event"] == "awaiting_input"
    assert json.loads(frame["data"])["thread_id"] == "t1"


def test_sse_survives_non_json_serialisable_values():
    """Plans carry dates; a raw json.dumps would raise mid-stream and truncate
    the response with no explanation."""
    from datetime import date

    frame = pr._sse({"type": "data", "when": date(2026, 8, 12)})
    assert "2026-08-12" in frame["data"]


# ── request models ───────────────────────────────────────────────────────────

def test_resume_defaults_to_approve():
    """A resume with no action must not silently adjust the plan."""
    assert pr.ResumeRequest(thread_id="t").action == "approve"


def test_stream_request_allows_a_caller_supplied_thread_id():
    """So a retry after a dropped connection resumes rather than starting a
    second, orphaned trip."""
    assert pr.PlannerRequest(query="q", thread_id="mine").thread_id == "mine"
    assert pr.PlannerRequest(query="q").thread_id is None


@pytest.mark.parametrize("action", ["approve", "adjust"])
def test_resume_carries_the_action_through(action):
    req = pr.ResumeRequest(thread_id="t", action=action, feedback="quieter")
    assert req.action == action
    assert req.feedback == "quieter"
