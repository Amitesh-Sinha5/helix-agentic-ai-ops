"""Billing endpoints: checkout, webhook, usage."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, Request

from app.billing import service
from app.config import get_settings
from app.core.cache import get_cache
from app.core.deps import CurrentUser, DBSession
from app.core.rate_limit import rate_limit_key
from app.schemas.billing import (
    CheckoutRequest,
    CheckoutResponse,
    SubscriptionOut,
    UsageResponse,
    WebhookAck,
)
from app.schemas.common import Tier

logger = logging.getLogger("helix.billing.router")

router = APIRouter(prefix="/billing", tags=["billing"])


@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(payload: CheckoutRequest, user: CurrentUser, db: DBSession) -> CheckoutResponse:
    """Start a Free -> Pro upgrade and return the URL to redirect the user to."""
    url, session_id, mode = await service.create_checkout_session(
        db, user, success_url=payload.success_url, cancel_url=payload.cancel_url
    )
    return CheckoutResponse(checkout_url=url, session_id=session_id, tier=payload.tier, mode=mode)


@router.post("/webhook", response_model=WebhookAck)
async def stripe_webhook(
    request: Request,
    db: DBSession,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> WebhookAck:
    """Receive Stripe subscription lifecycle events.

    Deliberately unauthenticated -- Stripe cannot present a JWT. The signature
    header is what proves the request is genuine, so it is verified before the
    payload is trusted or acted on.
    """
    payload = await request.body()
    event = service.verify_webhook(payload, stripe_signature)
    event_type, handled = await service.handle_event(db, event)
    return WebhookAck(received=True, event_type=event_type, handled=handled)


@router.get("/subscription", response_model=SubscriptionOut)
async def get_subscription(user: CurrentUser, db: DBSession) -> SubscriptionOut:
    subscription = await service.get_or_create_subscription(db, user)
    return SubscriptionOut(
        tier=Tier(subscription.tier),
        status=subscription.status,
        current_period_end=subscription.current_period_end,
        cancel_at_period_end=subscription.cancel_at_period_end,
        stripe_customer_id=subscription.stripe_customer_id,
    )


@router.get("/usage", response_model=UsageResponse)
async def get_usage(user: CurrentUser, db: DBSession) -> UsageResponse:
    """Current window usage against the tier limit.

    Reads the same Redis sorted set the rate limiter writes, so the number here
    is the number that will actually gate the next request.
    """
    settings = get_settings()
    subscription = await service.get_or_create_subscription(db, user)
    tier = subscription.tier if subscription.status in ("active", "trialing") else "free"
    limit = settings.tier_limit(tier)

    used, resets_in = await get_cache().sliding_window_usage(
        rate_limit_key(user.id), window_seconds=settings.rate_limit_window_seconds
    )
    unlimited = limit < 0
    return UsageResponse(
        tier=Tier(tier),
        used=used,
        limit=limit,
        remaining=-1 if unlimited else max(0, limit - used),
        window_seconds=settings.rate_limit_window_seconds,
        resets_in_seconds=resets_in,
        unlimited=unlimited,
    )


@router.post("/simulate-completion", response_model=SubscriptionOut)
async def simulate_completion(user: CurrentUser, db: DBSession, session_id: str) -> SubscriptionOut:
    """Complete a simulated checkout locally (no-op when Stripe is configured).

    This is what makes the upgrade flow demonstrable without Stripe keys: the
    frontend's success redirect calls it, and the user really does become Pro.
    """
    subscription = await service.complete_simulated_checkout(db, user, session_id)
    return SubscriptionOut(
        tier=Tier(subscription.tier),
        status=subscription.status,
        current_period_end=subscription.current_period_end,
        cancel_at_period_end=subscription.cancel_at_period_end,
        stripe_customer_id=subscription.stripe_customer_id,
    )
