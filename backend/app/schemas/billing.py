"""Billing / subscription schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import Tier


class CheckoutRequest(BaseModel):
    tier: Tier = Tier.pro
    success_url: str | None = None
    cancel_url: str | None = None


class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str
    tier: Tier
    mode: str = Field(default="stripe", description="stripe | simulated")


class SubscriptionOut(BaseModel):
    tier: Tier
    status: str
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    stripe_customer_id: str | None = None


class UsageResponse(BaseModel):
    tier: Tier
    used: int
    limit: int = Field(description="-1 means unlimited")
    remaining: int = Field(description="-1 means unlimited")
    window_seconds: int
    resets_in_seconds: int
    unlimited: bool = False


class WebhookAck(BaseModel):
    received: bool = True
    event_type: str | None = None
    handled: bool = False
