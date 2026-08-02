"""Code Review pod schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel, Severity, TraceEvent, UsageStats


class Verdict(str, Enum):
    approve = "approve"
    comment = "comment"
    request_changes = "request_changes"


class CodeIssue(BaseModel):
    severity: Severity
    category: str = "general"
    line: int | None = Field(default=None, ge=1)
    title: str
    explanation: str
    suggestion: str | None = None
    agent: str | None = Field(default=None, description="quality | security")

    @property
    def is_blocking(self) -> bool:
        return self.severity in (Severity.critical, Severity.high)


class AgentIssues(BaseModel):
    """Raw structured output from one reviewer agent."""

    issues: list[CodeIssue] = Field(default_factory=list)


class ReviewSummary(BaseModel):
    verdict: Verdict
    summary: str
    severity_counts: dict[str, int] = Field(default_factory=dict)
    top_recommendation: str | None = None


class CodeReviewRequest(BaseModel):
    code: str = Field(min_length=1, max_length=60_000)
    language: str = "python"
    filename: str | None = None
    context: str | None = Field(default=None, max_length=2000)


class CodeReviewResult(BaseModel):
    request_id: str
    review_id: str | None = None
    filename: str | None = None
    language: str
    verdict: Verdict
    summary: str
    issues: list[CodeIssue] = Field(default_factory=list)
    issue_count: int = 0
    blocking_count: int = 0
    severity_counts: dict[str, int] = Field(default_factory=dict)
    top_recommendation: str | None = None
    trace: list[TraceEvent] = Field(default_factory=list)
    usage: UsageStats


class CodeReviewOut(ORMModel):
    id: str
    filename: str | None = None
    language: str
    verdict: str
    summary: str
    issue_count: int
    blocking_count: int
    created_at: datetime
