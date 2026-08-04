"""Environment-driven application settings.

Every knob Helix has is defined here so that nothing downstream reads os.environ
directly. Defaults are chosen so a bare `uvicorn app.main:app` works with no
env file at all: SQLite, in-memory Redis, and the deterministic mock LLM.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LLMProvider = Literal["mock", "openai", "anthropic"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- App -----------------------------------------------------------------
    app_name: str = "Helix"
    environment: Literal["local", "ci", "staging", "production"] = "local"
    debug: bool = True

    # --- LLM -----------------------------------------------------------------
    llm_provider: LLMProvider = "mock"
    openai_api_key: str | None = None
    # Any OpenAI-compatible endpoint: Ollama, Groq, OpenRouter, Together,
    # LM Studio, Gemini's compat layer. Leave unset for OpenAI proper.
    # This is what makes free/local providers usable without a new backend.
    openai_base_url: str | None = None
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-5"
    openai_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1024
    llm_timeout_seconds: float = 60.0

    # Cost accounting. USD per 1k tokens; used by core.telemetry to attach a
    # price tag to every call so /observability/summary can report real spend.
    cost_per_1k_prompt_tokens: float = 0.003
    cost_per_1k_completion_tokens: float = 0.015

    # --- Embeddings ----------------------------------------------------------
    embedding_provider: Literal["mock", "openai"] = "mock"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 384
    cost_per_1k_embedding_tokens: float = 0.00002

    # --- Database ------------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./helix.db"
    db_echo: bool = False

    # --- Redis ---------------------------------------------------------------
    # Empty / unreachable falls back to an in-process fake so local dev and CI
    # never require a running Redis. See core.cache.
    redis_url: str = "redis://localhost:6379/0"
    redis_required: bool = False

    # --- Auth ----------------------------------------------------------------
    jwt_secret_key: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14

    # --- Rate limiting -------------------------------------------------------
    rate_limit_enabled: bool = True
    free_tier_daily_requests: int = 20
    pro_tier_daily_requests: int = -1  # -1 == unlimited
    rate_limit_window_seconds: int = 86_400

    # --- Semantic cache ------------------------------------------------------
    semantic_cache_enabled: bool = True
    semantic_cache_ttl_seconds: int = 3_600
    semantic_cache_threshold: float = 0.93
    semantic_cache_max_entries: int = 500

    # --- RAG -----------------------------------------------------------------
    chroma_persist_dir: str = "./.chroma"
    chunk_size: int = 800
    chunk_overlap: int = 120
    retrieval_top_k: int = 8  # candidates pulled from each retriever
    rerank_top_n: int = 4  # chunks handed to the answer agent
    rrf_k: int = 60  # reciprocal-rank-fusion smoothing constant
    max_retrieval_loops: int = 2  # agentic re-query budget
    min_retrieval_score: float = 0.15
    reranker_enabled: bool = False
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # --- Support triage ------------------------------------------------------
    classifier_path: str = "app/support/classifier.pkl"
    classifier_confidence_threshold: float = 0.55

    # --- Billing -------------------------------------------------------------
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_price_id_pro: str = "price_helix_pro_test"
    billing_success_url: str = "http://localhost:5173/billing?status=success"
    billing_cancel_url: str = "http://localhost:5173/billing?status=cancelled"

    # --- Quality gate --------------------------------------------------------
    quality_min_faithfulness: float = 0.70
    quality_min_answer_relevance: float = 0.70
    quality_min_context_precision: float = 0.60

    # --- CORS ----------------------------------------------------------------
    # NoDecode hands us the raw env string. Without it, pydantic-settings tries
    # to JSON-decode any complex-typed field *before* validators run, so a
    # perfectly reasonable `CORS_ORIGINS=https://a.com,https://b.com` raises a
    # parse error instead of reaching the validator below.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:5173"])

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """Accept either a JSON array or a comma-separated list."""
        if isinstance(v, str):
            text = v.strip()
            if text.startswith("["):
                import json

                try:
                    return json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"CORS_ORIGINS is not valid JSON: {exc}") from exc
            return [o.strip() for o in text.split(",") if o.strip()]
        return v

    @property
    def sync_database_url(self) -> str:
        """Alembic and other sync tooling need a non-async driver."""
        return (
            self.database_url.replace("+aiosqlite", "")
            .replace("+asyncpg", "+psycopg")
            .replace("postgresql+psycopg://", "postgresql+psycopg://")
        )

    def tier_limit(self, tier: str) -> int:
        return self.pro_tier_daily_requests if tier == "pro" else self.free_tier_daily_requests


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
