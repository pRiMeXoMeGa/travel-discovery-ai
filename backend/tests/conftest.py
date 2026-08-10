"""Shared pytest fixtures + heavy-dependency stubs for the backend test suite.

============================================================================
STUB CONTRACT — read this before writing tests against app.llm,
app.agents.*, app.routers.*, or anything that transitively imports
app.db / app.cache / app.vectorstore / app.embeddings.
============================================================================

`backend/app/*` imports four "heavy" or optional runtime dependencies:
fastembed (pulls in onnxruntime, ~200MB), qdrant-client, redis, asyncpg.
None of them are installed by `backend/requirements-dev.txt` — CI installs
only that file, not `backend/requirements.txt` — so importing `app.*`
modules directly would fail with ImportError.

To make `import app.whatever` work anyway, this file installs FAKE modules
into `sys.modules` for all four packages *before* anything under `app/` is
imported (this happens at conftest collection time, which pytest always
runs before it imports any test module in this directory). The fakes are
dumb, import-time-only stand-ins — they let real code run its normal
control flow without crashing, but they do **not** simulate real
database/vector-store/cache/embedding behaviour. Concretely:

  asyncpg
    `Pool`, `Connection` are placeholder classes; `create_pool(...)` is an
    async function returning a `Pool()` whose `.acquire()` gives an async
    context manager yielding a `Connection()` whose `.fetch()`/`.fetchrow()`/
    `.execute()` are no-ops returning `[]` / `None` / `None`.
    => `app.db.get_pool()` "works" but returns nothing useful. Any test
       that needs specific rows back MUST monkeypatch `app.db.get_pool`
       (or patch the specific module's imported `get_pool` name, e.g.
       `app.agents.retrieval.get_pool`) with your own async stub.

  redis (+ redis.asyncio)
    `Redis` is a placeholder class; `from_url(...)` returns a `Redis()`
    whose `.get`/`.set`/`.aclose` are async no-ops (get always returns
    None, i.e. an always-empty cache).
    => Caching is effectively disabled by default. Tests that need to
       assert cache hit/miss behaviour should monkeypatch
       `app.cache.get_redis` or `app.cache.cache_get`/`cache_set` directly.

  qdrant_client (+ qdrant_client.models, importable as `from qdrant_client
  import models`)
    `AsyncQdrantClient` is a placeholder class whose async methods
    (`search`, `query_points`, `close`, ...) are no-ops returning empty
    results. `qdrant_client.models` exposes `Filter`, `Condition`,
    `FieldCondition`, `MatchValue`, `Range` as permissive stub classes:
    each just stores whatever kwargs it's given as attributes (so
    `qmodels.Filter(must=[...], must_not=[...])` works, and code can read
    `.must`/`.must_not`/`.key`/`.match`/`.value`/`.range` back off the
    result for shape assertions). They also support `SomeStub | None`
    type-hint syntax since that works for any class in Python 3.10+.
    => Tests asserting real search *results* should monkeypatch
       `app.vectorstore.get_qdrant` with a fake client returning
       whatever hits you want.

  fastembed
    `TextEmbedding(model_name=...)` is a placeholder whose `.embed(texts)`
    yields one deterministic all-zero 384-dim vector (via a `.tolist()`
    shim matching the real ONNX return shape) per input text.
    => `app.embeddings.embed_query` / `embed_texts` run end-to-end and
       return real Python lists, just all-zero. Tests that care about
       actual vector content should monkeypatch `app.embeddings.embed_query`
       / `embed_texts` directly rather than relying on the stub's output.

If a test needs the REAL package (e.g. a future integration test), don't
fight this file — install the real dependency and delete/guard the
relevant stub registration, or monkeypatch `sys.modules` back within that
one test.

----------------------------------------------------------------------------
Network safety (zero LLM quota / zero network calls from the test suite)
----------------------------------------------------------------------------
An autouse fixture below patches `httpx.AsyncClient.send` and
`httpx.Client.send` — the common choke point both `.request()`/`.get()`/
`.post()` and `.stream()` funnel through — to raise `RuntimeError`
immediately for every test. Any code path that would make a real HTTP call
(Gemini/Anthropic REST in `app.llm`, or anything else) fails the test
loudly and instantly instead of silently spending quota, hanging, or
flaking on network access in CI. Tests that want to exercise `app.llm`'s
request-building / retry / parsing logic should monkeypatch at the
`app.llm._gemini_complete` / `_gemini_stream` / `_anthropic_complete` /
`_anthropic_stream` level (or `httpx.AsyncClient.send` back within that one
test) rather than rely on real network I/O ever running here.

`GEMINI_API_KEY` and `ANTHROPIC_API_KEY` are also forced to dummy,
obviously-fake values (not merely deleted) before `app.config.settings` is
ever constructed, so:
  * a real key in a developer's local `.env` can never leak into a test run
    (`Settings` only falls back to the `.env` file for a field that is NOT
    already present in `os.environ`, and this module sets them first), and
  * `app.llm`'s "key not configured" guards don't short-circuit before a
    test even reaches the code path it's trying to exercise.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

# ── 1) Force dummy provider keys BEFORE app.config.settings is constructed ──
# (app.config.settings = Settings() runs at import time of app.config, the
# first time anything imports it — which must be after this module runs.)
os.environ["GEMINI_API_KEY"] = "test-dummy-gemini-key-not-real"
os.environ["ANTHROPIC_API_KEY"] = "test-dummy-anthropic-key-not-real"


# ── 2) Heavy-dependency stubs, installed into sys.modules ───────────────────
class _StubAttrs:
    """Generic permissive stand-in: accepts any kwargs, stores them as attrs.

    Used for qdrant_client.models.* — real code both constructs these
    (`qmodels.FieldCondition(key=..., match=...)`) and uses them in
    `X | None` type hints, both of which work for any plain class.
    """

    def __init__(self, *args, **kwargs) -> None:
        self.__dict__.update(kwargs)
        if args:
            self.__dict__["_args"] = args

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"{type(self).__name__}({self.__dict__!r})"


def _make_stub_class(name: str) -> type:
    return type(name, (_StubAttrs,), {})


def _install_qdrant_stub() -> None:
    models_mod = types.ModuleType("qdrant_client.models")
    for cls_name in ("Filter", "Condition", "FieldCondition", "MatchValue", "Range"):
        setattr(models_mod, cls_name, _make_stub_class(cls_name))

    class _StubAsyncQdrantClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def close(self) -> None:
            pass

        async def search(self, *args, **kwargs) -> list:
            return []

        async def query_points(self, *args, **kwargs):
            return types.SimpleNamespace(points=[])

        def __getattr__(self, name: str):
            async def _noop(*args, **kwargs):
                return None

            return _noop

    client_mod = types.ModuleType("qdrant_client")
    client_mod.AsyncQdrantClient = _StubAsyncQdrantClient
    client_mod.models = models_mod

    sys.modules["qdrant_client"] = client_mod
    sys.modules["qdrant_client.models"] = models_mod


def _install_redis_stub() -> None:
    class _StubRedis:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def get(self, key):
            return None

        async def set(self, key, value, ex=None):
            return True

        async def aclose(self) -> None:
            pass

        def __getattr__(self, name: str):
            async def _noop(*args, **kwargs):
                return None

            return _noop

    def _from_url(*args, **kwargs):
        return _StubRedis()

    asyncio_mod = types.ModuleType("redis.asyncio")
    asyncio_mod.Redis = _StubRedis
    asyncio_mod.from_url = _from_url

    redis_mod = types.ModuleType("redis")
    redis_mod.asyncio = asyncio_mod

    sys.modules["redis"] = redis_mod
    sys.modules["redis.asyncio"] = asyncio_mod


def _install_asyncpg_stub() -> None:
    class _StubConnection:
        async def fetch(self, *args, **kwargs) -> list:
            return []

        async def fetchrow(self, *args, **kwargs):
            return None

        async def execute(self, *args, **kwargs):
            return None

        async def set_type_codec(self, *args, **kwargs) -> None:
            pass

    class _StubAcquireCtx:
        def __init__(self, conn: "_StubConnection") -> None:
            self._conn = conn

        async def __aenter__(self) -> "_StubConnection":
            return self._conn

        async def __aexit__(self, *exc_info) -> bool:
            return False

    class _StubPool:
        def acquire(self) -> "_StubAcquireCtx":
            return _StubAcquireCtx(_StubConnection())

        async def close(self) -> None:
            pass

    async def _create_pool(*args, **kwargs):
        return _StubPool()

    asyncpg_mod = types.ModuleType("asyncpg")
    asyncpg_mod.Pool = _StubPool
    asyncpg_mod.Connection = _StubConnection
    asyncpg_mod.create_pool = _create_pool

    sys.modules["asyncpg"] = asyncpg_mod


def _install_fastembed_stub() -> None:
    class _StubVector:
        def __init__(self, dim: int = 384) -> None:
            self._dim = dim

        def tolist(self) -> list[float]:
            return [0.0] * self._dim

    class _StubTextEmbedding:
        def __init__(self, model_name: str | None = None, **kwargs) -> None:
            self.model_name = model_name

        def embed(self, texts):
            for _ in texts:
                yield _StubVector()

    fastembed_mod = types.ModuleType("fastembed")
    fastembed_mod.TextEmbedding = _StubTextEmbedding

    sys.modules["fastembed"] = fastembed_mod


_install_qdrant_stub()
_install_redis_stub()
_install_asyncpg_stub()
_install_fastembed_stub()


# ── 3) Network safety net: no test may perform a real HTTP call ─────────────
@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard-fail any attempt to send a real request via httpx.

    Patches the `send()` choke point that both `AsyncClient`/`Client`'s
    `.get()/.post()/.request()` AND `.stream()` funnel through, so no code
    path — including app.llm's Gemini/Anthropic REST calls — can reach the
    network from within a test. See module docstring for how tests that
    want to exercise app.llm's request logic should mock around this.
    """
    import httpx

    async def _blocked_async_send(self, *args, **kwargs):
        raise RuntimeError(
            "Network call blocked in tests (httpx.AsyncClient.send). "
            "Mock the LLM/API call instead of hitting the network — see "
            "the conftest.py module docstring."
        )

    def _blocked_sync_send(self, *args, **kwargs):
        raise RuntimeError(
            "Network call blocked in tests (httpx.Client.send). "
            "Mock the LLM/API call instead of hitting the network — see "
            "the conftest.py module docstring."
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", _blocked_async_send, raising=True)
    monkeypatch.setattr(httpx.Client, "send", _blocked_sync_send, raising=True)
