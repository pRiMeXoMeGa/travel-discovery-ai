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

    # Cache (Redis / Upstash)
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 3600

    # LLM
    llm_provider: str = "gemini"  # "gemini" | "anthropic"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-haiku-4-5-20251001"

    # Embeddings (local fastembed/ONNX)
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    # App
    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
