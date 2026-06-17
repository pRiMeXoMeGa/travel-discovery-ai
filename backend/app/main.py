"""FastAPI entrypoint: CORS, lifespan, routers, health + observability."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .cache import close_redis
from .config import settings
from .db import close_pool, get_pool
from .routers import agents, listings, search
from .vectorstore import close_qdrant


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the Postgres pool on startup; fail fast if misconfigured.
    await get_pool()
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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
