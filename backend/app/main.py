"""FastAPI entrypoint: CORS, lifespan, routers, health + observability."""
import logging
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .cache import close_redis
from .config import settings
from .db import close_pool, get_pool
from .routers import agents, listings, memory, search
from .vectorstore import close_qdrant

logger = logging.getLogger(__name__)

# WS2: platform-as-MCP-server, mounted at /mcp below. Guarded by try/except
# ImportError, not a bare top-level import, because `fastmcp` lives only in
# `backend/requirements.txt`, not `requirements-dev.txt` — real CI
# (.github/workflows/ci.yml) installs only the latter, and `app.main` is
# imported by every test in the suite via router registration. Same reason
# `app.memory.store` (mem0) is deferred into the lifespan function body
# rather than imported at module top. Under the full Docker image
# (`backend/requirements.txt`, fastmcp pinned and installed), this import
# succeeds and /mcp is mounted for real; under requirements-dev.txt-only
# environments it degrades to "no /mcp route", not a crash — every existing
# HTTP endpoint is unaffected either way.
try:
    from .mcp_server.auth import BearerAuthMiddleware, RateLimitMiddleware
    from .mcp_server.server import build_mcp_app

    mcp_app = build_mcp_app()
except ImportError as exc:  # pragma: no cover - exercised by CI's requirements-dev-only env
    logger.warning("MCP server unavailable (fastmcp not installed); /mcp will not be mounted: %s", exc)
    mcp_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        if mcp_app is not None:
            # Chains fastmcp's own lifespan (initialises its session
            # manager) into FastAPI's. Skipping this makes every /mcp
            # request fail with an opaque "session not initialised" error —
            # see mcp_server/server.py's module docstring.
            await stack.enter_async_context(mcp_app.lifespan(app))

        # Warm the Postgres pool on startup; fail fast if misconfigured.
        await get_pool()

        # Memory (WS1) is initialized eagerly here, not lazily on first
        # concierge request, so its startup cost (mem0 client construction +
        # the loud local/Gemini check below) is paid once at boot rather
        # than stalling whichever user's request happens to arrive first.
        if settings.memory_enabled:
            # Imported here, not at module top: `app.memory.store` pulls in
            # mem0 (~117MB of imports measured), and this module is
            # imported by every test in the suite via `app.main` / router
            # registration — most of which never touch memory and must not
            # pay that cost.
            from .memory import store as memory_store

            try:
                # init_memory() calls assert_local_and_gemini() internally,
                # which raises RuntimeError if mem0 silently fell back to a
                # remote, unconfigured provider (missing/misspelled key).
                # That is a misconfiguration — sending traveller data to an
                # unintended third party and burning quota — not an outage,
                # so it must fail startup loudly rather than let the app
                # come up "healthy".
                memory_store.init_memory()
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001 - outage, not misconfiguration
                # Anything else here (Qdrant unreachable, connection
                # refused, timeout, ...) is mem0 being temporarily
                # unavailable, not wired wrong. Memory is an enhancement on
                # top of search/listings/concierge (see store.py's
                # degradation contract: every runtime call already returns
                # empty results on failure) — a mem0 outage must not take
                # the whole API down, so log and continue with memory
                # degraded instead of raising.
                logger.warning(
                    "memory init failed; continuing with memory degraded: %s", exc
                )

        yield
        await close_pool()
        await close_qdrant()
        await close_redis()


app = FastAPI(title="Travel Discovery AI", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(search.router)
app.include_router(listings.router)
app.include_router(agents.router)
app.include_router(memory.router)

if mcp_app is not None:
    # Auth wraps rate-limit wraps the FastMCP app, in that order — see
    # mcp_server/auth.py's module docstring for why (auth must reject a bad
    # key before the request can consume rate-limit budget).
    app.mount(
        "/mcp",
        BearerAuthMiddleware(
            RateLimitMiddleware(mcp_app, rpm=settings.mcp_llm_rpm),
            settings.mcp_api_key,
        ),
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
