"""Doc Q&A request/response models, including agent-internal contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import COLLECTION_NAME, ORMModel, TraceEvent, UsageStats


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
class IngestRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1)
    source: str | None = None
    collection: str = COLLECTION_NAME
    metadata: dict = Field(default_factory=dict)


class IngestResponse(BaseModel):
    document_id: str
    title: str
    chunk_count: int
    char_count: int
    collection: str
    reingested: bool = False
    cache_invalidated: int = 0


class DocumentOut(ORMModel):
    id: str
    title: str
    source: str | None = None
    collection: str
    chunk_count: int
    char_count: int
    status: str
    created_at: datetime


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #
class RetrievedChunk(BaseModel):
    chunk_id: str
    document_id: str
    text: str
    source: str | None = None
    document_title: str | None = None
    score: float = 0.0
    vector_rank: int | None = None
    keyword_rank: int | None = None
    fused_score: float = 0.0
    rerank_score: float | None = None


class Citation(BaseModel):
    index: int
    document_id: str
    document_title: str | None = None
    source: str | None = None
    snippet: str


# --------------------------------------------------------------------------- #
# Agent-internal structured outputs (validated by core.guardrails)
# --------------------------------------------------------------------------- #
class SufficiencyVerdict(BaseModel):
    sufficient: bool
    confidence: float = 0.0
    reason: str = ""
    missing_information: list[str] = Field(default_factory=list)


class ReformulatedQuery(BaseModel):
    query: str
    rationale: str = ""


class GroundednessVerdict(BaseModel):
    grounded: bool
    score: float = 0.0
    reason: str = ""
    # Scored in the same validator call as groundedness: the node already holds
    # the question, answer and context, so relevance costs nothing extra.
    answer_relevance: float = 0.0


class CritiqueResult(BaseModel):
    revised_answer: str
    changed: bool = False
    critique: str = ""


class ToolDecision(BaseModel):
    tool: str | None = None
    arguments: dict = Field(default_factory=dict)


class ToolInvocation(BaseModel):
    tool: str
    arguments: dict = Field(default_factory=dict)
    result: dict = Field(default_factory=dict)
    latency_ms: float = 0.0


# --------------------------------------------------------------------------- #
# Query
# --------------------------------------------------------------------------- #
class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    collection: str = COLLECTION_NAME
    top_k: int | None = Field(default=None, ge=1, le=20)
    session_id: str | None = Field(default=None, description="Enables multi-turn follow-ups")
    use_cache: bool = True
    stream_trace: bool = True
    self_critique: bool = True


class QueryResponse(BaseModel):
    request_id: str
    question: str
    answer: str
    found: bool
    citations: list[Citation] = Field(default_factory=list)
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    retrieval_loops: int = 1
    reformulated_queries: list[str] = Field(default_factory=list)
    groundedness: GroundednessVerdict | None = None
    critique: str | None = None
    tool_invocations: list[ToolInvocation] = Field(default_factory=list)
    trace: list[TraceEvent] = Field(default_factory=list)
    usage: UsageStats


class FeedbackRequest(BaseModel):
    request_id: str
    rating: int = Field(description="+1 for thumbs up, -1 for thumbs down")
    question: str | None = None
    answer: str | None = None
    comment: str | None = Field(default=None, max_length=2000)
    pod: str = "doc_qa"

    @property
    def normalised_rating(self) -> int:
        return 1 if self.rating > 0 else -1


class FeedbackResponse(BaseModel):
    id: str
    rating: int
    recorded: bool = True
