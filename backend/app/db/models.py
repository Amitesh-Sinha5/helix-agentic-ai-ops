"""SQLAlchemy models for every Helix table.

JSON columns use the generic `sqlalchemy.JSON` type so the same schema runs on
SQLite (local dev, CI) and Postgres (compose, production) without a branch.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamped, UUIDPrimaryKey

# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


class User(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="user", nullable=False)  # user | admin
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    subscription: Mapped[Subscription | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class RefreshToken(UUIDPrimaryKey, Timestamped, Base):
    """Refresh tokens are stored hashed and rotated on every use.

    A reused (already-rotated) token is treated as theft: `replaced_by` lets the
    auth router detect reuse and revoke the whole family.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[str | None] = mapped_column(String(128))
    user_agent: Mapped[str | None] = mapped_column(String(300))

    user: Mapped[User] = relationship(back_populates="refresh_tokens")


# --------------------------------------------------------------------------- #
# Pod 1 -- Doc Q&A
# --------------------------------------------------------------------------- #


class Document(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "documents"

    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str | None] = mapped_column(String(500))
    content_type: Mapped[str] = mapped_column(String(50), default="text/plain", nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    collection: Mapped[str] = mapped_column(String(100), default="documents", nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ready", nullable=False)
    doc_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)


class Feedback(UUIDPrimaryKey, Timestamped, Base):
    """Thumbs up/down on an answer, harvested back into the golden dataset."""

    __tablename__ = "feedback"

    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    pod: Mapped[str] = mapped_column(String(40), default="doc_qa", nullable=False)
    question: Mapped[str | None] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)  # +1 / -1
    comment: Mapped[str | None] = mapped_column(Text)
    promoted_to_golden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


# --------------------------------------------------------------------------- #
# Pod 2 -- Code Review
# --------------------------------------------------------------------------- #


class CodeReview(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "code_reviews"

    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    filename: Mapped[str | None] = mapped_column(String(300))
    language: Mapped[str] = mapped_column(String(40), default="python", nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[str] = mapped_column(String(30), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    issues: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    issue_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocking_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# --------------------------------------------------------------------------- #
# Pod 3 -- Support Triage
# --------------------------------------------------------------------------- #


class Ticket(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "tickets"

    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    customer_email: Mapped[str | None] = mapped_column(String(320))
    priority: Mapped[str] = mapped_column(String(20), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # "trained_model" when the sklearn classifier was confident enough to answer
    # on its own (zero LLM cost), "llm_fallback" when it was not.
    classification_path: Mapped[str] = mapped_column(String(30), nullable=False)
    draft_response: Mapped[str | None] = mapped_column(Text)
    escalate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    escalation_reason: Mapped[str | None] = mapped_column(Text)
    suggested_owner: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="triaged", nullable=False)
    kb_sources: Mapped[list] = mapped_column(JSON, default=list, nullable=False)


# --------------------------------------------------------------------------- #
# Observability
# --------------------------------------------------------------------------- #


class RequestLog(UUIDPrimaryKey, Base):
    """One row per agent/LLM operation. The raw material for /observability/summary."""

    __tablename__ = "request_logs"

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    pod: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(200))

    provider: Mapped[str | None] = mapped_column(String(30))
    model: Mapped[str | None] = mapped_column(String(80))
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retrieval_loops: Mapped[int | None] = mapped_column(Integer)
    classification_path: Mapped[str | None] = mapped_column(String(30))
    faithfulness: Mapped[float | None] = mapped_column(Float)
    answer_relevance: Mapped[float | None] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String(20), default="ok", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (Index("ix_request_logs_pod_created", "pod", "created_at"),)


# --------------------------------------------------------------------------- #
# Billing
# --------------------------------------------------------------------------- #


class Subscription(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "subscriptions"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tier: Mapped[str] = mapped_column(String(20), default="free", nullable=False)  # free | pro
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(120), index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(120), index=True)
    stripe_checkout_session_id: Mapped[str | None] = mapped_column(String(200))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[User] = relationship(back_populates="subscription")

    __table_args__ = (UniqueConstraint("user_id", name="uq_subscriptions_user"),)
