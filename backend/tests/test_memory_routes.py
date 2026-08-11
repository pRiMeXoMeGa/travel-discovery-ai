"""Tests for the WS1 memory router (backend/app/routers/memory.py).

`app.memory.store.forget` is monkeypatched in every test — zero network,
zero LLM quota, zero real mem0/Qdrant/Gemini calls.

mem0 is stubbed into `sys.modules` *before* `app.memory.store` (and
therefore `app.main`, which registers the memory router) is ever imported —
mirroring `tests/test_memory_store.py`. This matters even though the Docker
image used to reproduce CI locally has the real `mem0ai` package baked in
(via `requirements.txt`): the actual CI job installs only
`requirements-dev.txt` (see `conftest.py`'s docstring), which does NOT
include mem0, so an unstubbed `import mem0` would fail there. Importing
`app.memory.store` at module import time (rather than inside a test/fixture,
after the stub is in place) would defeat this — see the module docstring in
`app/routers/memory.py` for why the router itself only imports it lazily,
inside the request handler.
"""
from __future__ import annotations

import sys
import types

import httpx
import pytest
from fastapi.testclient import TestClient

# conftest.py's autouse `_block_network` fixture patches httpx.Client.send /
# httpx.AsyncClient.send to always raise, to stop app.llm from ever making a
# real outbound call in tests. TestClient is ITSELF an httpx.Client (talking
# to `app` in-process over ASGITransport — no socket, no network, just direct
# Python calls), so that same patch would also block it. Capture the real
# implementation here, at module import time, before any test's autouse
# fixture has run, so the `client` fixture below can restore it just for
# TestClient's use.
_REAL_SYNC_SEND = httpx.Client.send


def _stub_mem0() -> None:
    """Install a bare-bones fake `mem0` package, matching test_memory_store.py.

    Guarded by the `sys.modules` check so this is a no-op (and does not
    clobber a real, already-imported mem0) if another test module already
    installed the same stub — tests in this directory can run in either
    order.
    """
    if "mem0" not in sys.modules:
        mem0 = types.ModuleType("mem0")

        class _Memory:
            @classmethod
            def from_config(cls, _config):  # pragma: no cover - never called here
                return cls()

        mem0.Memory = _Memory
        sys.modules["mem0"] = mem0

    if "mem0.embeddings.base" not in sys.modules:
        base = types.ModuleType("mem0.embeddings.base")

        class EmbeddingBase:
            def __init__(self, config=None):
                self.config = config

        base.EmbeddingBase = EmbeddingBase
        sys.modules["mem0.embeddings"] = types.ModuleType("mem0.embeddings")
        sys.modules["mem0.embeddings.base"] = base


_stub_mem0()

from app.config import settings  # noqa: E402 - must follow the mem0 stub above
from app.main import app  # noqa: E402


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """A TestClient that runs the real app lifespan with memory disabled.

    `with TestClient(...)` triggers FastAPI's real startup/shutdown, which
    would otherwise call `store.init_memory()` -> `Memory.from_config()` on
    our bare `_Memory` stub (no `embedding_model`/`llm` attributes) and hit
    `assert_local_and_gemini`'s loud-fail path — correct behaviour for that
    function, but irrelevant noise for tests that only care about this
    route's status-code contract. Disabling `settings.memory_enabled` skips
    that branch entirely, exactly as `app/main.py`'s lifespan is written to.
    """
    monkeypatch.setattr(settings, "memory_enabled", False)
    # Un-block httpx.Client.send for this fixture's scope only (see the
    # `_REAL_SYNC_SEND` note above) — pytest runs autouse fixtures before
    # explicitly-requested ones of the same scope, so this reliably applies
    # after conftest's block and is reverted before it on teardown.
    monkeypatch.setattr(httpx.Client, "send", _REAL_SYNC_SEND)
    with TestClient(app) as c:
        yield c


def test_route_registered_at_exact_frontend_path():
    """frontend/lib/api.ts::forgetMemory calls DELETE /api/memory/{id}."""
    paths = {route.path for route in app.routes}
    assert "/api/memory/{memory_id}" in paths


def test_successful_delete_returns_2xx(client, monkeypatch):
    from app.memory import store

    async def _forget_ok(memory_id: str) -> bool:
        assert memory_id == "mem-123"
        return True

    monkeypatch.setattr(store, "forget", _forget_ok)

    res = client.delete("/api/memory/mem-123?user_id=user-abc")
    assert 200 <= res.status_code < 300


def test_failed_delete_returns_non_2xx(client, monkeypatch):
    """Optimistic UI: the panel only restores the hidden row on a non-2xx."""
    from app.memory import store

    async def _forget_fail(memory_id: str) -> bool:
        return False

    monkeypatch.setattr(store, "forget", _forget_fail)

    res = client.delete("/api/memory/mem-404?user_id=user-abc")
    assert not (200 <= res.status_code < 300)


def test_delete_without_user_id_query_param_still_routes(client, monkeypatch):
    """forgetMemory() omits `user_id` entirely when localStorage has none yet."""
    from app.memory import store

    async def _forget_ok(memory_id: str) -> bool:
        return True

    monkeypatch.setattr(store, "forget", _forget_ok)

    res = client.delete("/api/memory/mem-123")
    assert 200 <= res.status_code < 300
