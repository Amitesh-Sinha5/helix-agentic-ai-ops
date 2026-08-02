"""Shared response envelopes and enums."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Severity(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


class Priority(str, Enum):
    urgent = "urgent"
    high = "high"
    medium = "medium"
    low = "low"


class TicketCategory(str, Enum):
    billing = "billing"
    bug = "bug"
    account = "account"
    how_to = "how_to"
    feature_request = "feature_request"
    general = "general"


class Tier(str, Enum):
    free = "free"
    pro = "pro"


# Chroma enforces this shape on collection names; validating at the API
# boundary turns a 500 from deep inside the vector store into a clear 422.
COLLECTION_NAME = Field(
    default="documents",
    min_length=3,
    max_length=63,
    pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,61}[a-zA-Z0-9]$",
    description="3-63 chars: alphanumerics, underscores and hyphens, starting and ending alphanumeric.",
)


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "helix-backend"
    version: str = "1.0.0"
    llm_provider: str = "mock"
    database: str = "unknown"
    redis: str = "unknown"


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None
    request_id: str | None = None


class UsageStats(BaseModel):
    """Per-request cost/latency footprint, returned inline with pod responses."""

    request_id: str
    latency_ms: float
    llm_calls: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cache_hit: bool = False


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int = 50
    offset: int = 0


class TraceEvent(BaseModel):
    """A structured agent-step event streamed over the Phase 7 WebSocket.

    Deliberately structured rather than a status string: the frontend renders an
    expandable reasoning trace from these fields.
    """

    request_id: str
    pod: str
    node: str
    phase: str = Field(description="start | finish | error")
    sequence: int = 0
    duration_ms: float | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class EscalationEvent(BaseModel):
    """Pushed to admins over /ws/admin/escalations the moment triage escalates."""

    ticket_id: str
    request_id: str
    subject: str
    priority: Priority
    category: TicketCategory
    reason: str
    suggested_owner: str | None = None
    customer_email: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
