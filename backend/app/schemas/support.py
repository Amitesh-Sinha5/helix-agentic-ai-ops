"""Support Triage pod schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMModel, Priority, TicketCategory, TraceEvent, UsageStats


class Classification(BaseModel):
    priority: Priority
    category: TicketCategory
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""


class ClassificationResult(Classification):
    """Classification plus which path produced it.

    `path` is the headline metric for this pod: `trained_model` means the local
    scikit-learn classifier was confident enough to decide on its own, at zero
    LLM cost; `llm_fallback` means it was not.
    """

    path: str = "trained_model"
    model_confidence: float | None = None
    probabilities: dict[str, float] = Field(default_factory=dict)


class EscalationDecision(BaseModel):
    escalate: bool
    reason: str = ""
    suggested_owner: str | None = None


class KBSource(BaseModel):
    document_id: str
    title: str | None = None
    snippet: str
    score: float = 0.0


class TriageRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20_000)
    customer_email: EmailStr | None = None
    collection: str = Field(
        default="knowledge_base",
        min_length=3,
        max_length=63,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,61}[a-zA-Z0-9]$",
    )
    stream_trace: bool = True


class TriageResponse(BaseModel):
    request_id: str
    ticket_id: str
    subject: str
    priority: Priority
    category: TicketCategory
    confidence: float
    classification_path: str
    draft_response: str
    escalate: bool
    escalation_reason: str | None = None
    suggested_owner: str | None = None
    kb_sources: list[KBSource] = Field(default_factory=list)
    trace: list[TraceEvent] = Field(default_factory=list)
    usage: UsageStats


class TicketOut(ORMModel):
    id: str
    subject: str
    priority: str
    category: str
    confidence: float
    classification_path: str
    escalate: bool
    status: str
    created_at: datetime
