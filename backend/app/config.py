from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Load the repo-root .env regardless of the process CWD (uvicorn is commonly
# launched from backend/). Real environment variables still take precedence,
# so docker-compose's injected vars override this in containers.
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """Central config, loaded from environment (.env). See ../.env.example."""

    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    # Relational (Postgres / Neon) — asyncpg DSN
    database_url: str = "postgresql://travel:travel@localhost:5432/travel"

    # Vector store (Qdrant / Qdrant Cloud)
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection_listings: str = "listings"
    qdrant_collection_reviews: str = "reviews"
    # Per-property review-summary vectors (one point per listing, payload
    # {"listing_id": ...}). Built by ingestion `stage_embed_summaries`, shipped
    # in the snapshot, and queried by agents/retrieval.py (WS0-H). A restored
    # *older* snapshot may not contain it — retrieval degrades to listings-only.
    qdrant_collection_summaries: str = "summaries"
    # Traveller/trip memory vectors (WS1). MUST be 384-dim like the others —
    # mem0 creates it on first write, and a dims mismatch surfaces later as an
    # opaque Qdrant shape error on upsert, never a useful message.
    qdrant_collection_memories: str = "memories"

    # Reranking (WS4) — OFF by default, and that is a measurement not a
    # preference: the smallest supported cross-encoder costs +156 MB resident
    # and 20.8s to load, against ~33 MB of headroom on the 512 MB instance
    # (the app already peaks at ~479 MB). See app/rerank.py. Enable only on an
    # instance with >=1 GB. `scripts/rerank_eval.py` measures the quality delta
    # offline without loading the model into the API process.
    rerank_enabled: bool = False
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    # Retrieve wide, rerank narrow. 50 is the plan's figure; it is also roughly
    # where latency stops being tolerable on one vCPU (1064 ms measured).
    rerank_candidates: int = 50

    # Memory (WS1)
    memory_enabled: bool = True
    # mem0 ships PostHog analytics that phone home on import. Off by default:
    # this is a portfolio app handling travellers' stated preferences, and
    # third-party telemetry on that is not a default anyone opted into.
    mem0_telemetry: bool = False

    # Cache (Redis / Upstash)
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 3600

    # LLM
    llm_provider: str = "gemini"  # "gemini" | "anthropic"
    gemini_api_key: str | None = None
    # Production value, per render.yaml and .env.example (WS0-G drift fix).
    gemini_model: str = "gemini-3.1-flash-lite"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-haiku-4-5-20251001"

    # Embeddings (local fastembed/ONNX)
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    # MCP server (WS2) — platform exposed as tools at /mcp, mounted in main.py.
    # Bearer auth is enforced as ASGI middleware (mcp_server/auth.py). Unset
    # is NOT "auth disabled" — BearerAuthMiddleware fails CLOSED (503) when
    # this is empty, because two of the six tools spend Gemini quota and a
    # public unauthenticated MCP endpoint would leak that budget. Set in the
    # Render dashboard as a `sync: false` secret; never commit a real value.
    mcp_api_key: str | None = None
    # RPM cap applied only to the two LLM-backed tools (synthesize_reviews,
    # plan_itinerary) — see mcp_server/auth.py::RateLimitMiddleware. The four
    # zero-LLM tools are uncapped here (Postgres/Qdrant already have their
    # own timeouts/pooling limits).
    mcp_llm_rpm: int = 10

    # App
    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
