"""Observability schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PodStats(BaseModel):
    pod: str
    requests: int = 0
    llm_calls: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    error_rate: float = 0.0
    cache_hits: int = 0
    cache_hit_rate: float = 0.0
    estimated_cost_saved_usd: float = 0.0
    avg_retrieval_loops: float | None = None
    avg_faithfulness: float | None = None
    avg_answer_relevance: float | None = None
    trained_model_share: float | None = Field(
        default=None, description="Support triage: fraction handled with no LLM call"
    )


class ObservabilitySummary(BaseModel):
    window_hours: int
    generated_at: str
    total_requests: int = 0
    total_llm_calls: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    estimated_cost_saved_usd: float = 0.0
    overall_cache_hit_rate: float = 0.0
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    error_rate: float = 0.0
    avg_faithfulness: float | None = None
    avg_answer_relevance: float | None = None
    pods: list[PodStats] = Field(default_factory=list)
    top_operations: list[dict] = Field(default_factory=list)


class QualityScore(BaseModel):
    faithfulness: float | None = None
    answer_relevance: float | None = None
    context_precision: float | None = None
