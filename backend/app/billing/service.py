"""Stripe integration, with a simulated mode for local dev and CI.

When `STRIPE_SECRET_KEY` is unset the service runs in `simulated` mode: checkout
returns a local URL and the webhook accepts unsigned payloads. That keeps the
whole upgrade flow clickable and testable without Stripe credentials, while the
code path that Stripe actually drives -- signature verification, event handling,
subscription state transitions -- stays exactly the same.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.errors import HelixError, UpstreamError
from app.db.models import Subscription, User

logger = logging.getLogger("helix.billing")

HANDLED_EVENTS = (
    "checkout.session.completed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
)


class BillingError(HelixError):
    """Billing operation failed."""

    status_code = 400
    code = "billing_error"


def stripe_enabled() -> bool:
    return bool(get_settings().stripe_secret_key)


def _stripe():
    import stripe as stripe_sdk

    stripe_sdk.api_key = get_settings().stripe_secret_key
    return stripe_sdk


async def get_or_create_subscription(db: AsyncSession, user: User) -> Subscription:
    result = await db.execute(select(Subscription).where(Subscription.user_id == user.id))
    subscription = result.scalar_one_or_none()
    if subscription is None:
        subscription = Subscription(user_id=user.id, tier="free", status="active")
        db.add(subscription)
        await db.flush()
    return subscription


async def create_checkout_session(
    db: AsyncSession, user: User, *, success_url: str | None = None, cancel_url: str | None = None
) -> tuple[str, str, str]:
    """Create a Pro checkout session. Returns (url, session_id, mode)."""
    settings = get_settings()
    subscription = await get_or_create_subscription(db, user)
    success = success_url or settings.billing_success_url
    cancel = cancel_url or settings.billing_cancel_url

    if not stripe_enabled():
        # Simulated: mint a session id the local webhook can complete.
        session_id = f"cs_test_sim_{secrets.token_urlsafe(16)}"
        subscription.stripe_checkout_session_id = session_id
        logger.info("Simulated checkout session %s for user %s", session_id, user.id)
        return (
            f"{success}&simulated=true&session_id={session_id}",
            session_id,
            "simulated",
        )

    stripe = _stripe()
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": settings.stripe_price_id_pro, "quantity": 1}],
            success_url=f"{success}&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=cancel,
            customer=subscription.stripe_customer_id or None,
            customer_email=None if subscription.stripe_customer_id else user.email,
            # client_reference_id is how the webhook maps the completed session
            # back to a Helix user without trusting anything in the request.
            client_reference_id=user.id,
            metadata={"helix_user_id": user.id, "tier": "pro"},
        )
    except Exception as exc:
        logger.exception("Stripe checkout creation failed")
        raise UpstreamError(f"Could not create a checkout session: {exc}") from exc

    subscription.stripe_checkout_session_id = session.id
    return session.url, session.id, "stripe"


def verify_webhook(payload: bytes, signature: str | None) -> dict[str, Any]:
    """Verify and parse a webhook event.

    In simulated mode the JSON is parsed without verification. With a webhook
    secret configured, an invalid or missing signature is rejected -- webhook
    endpoints are unauthenticated, so the signature *is* the authentication.
    """
    settings = get_settings()
    if not settings.stripe_webhook_secret:
        import json

        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise BillingError("Malformed webhook payload") from exc

    stripe = _stripe()
    try:
        event = stripe.Webhook.construct_event(payload, signature, settings.stripe_webhook_secret)
    except Exception as exc:
        logger.warning("Rejected webhook with bad signature: %s", exc)
        raise BillingError(f"Invalid webhook signature: {exc}") from exc
    return event if isinstance(event, dict) else event.to_dict_recursive()


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


async def _find_subscription(db: AsyncSession, data: dict[str, Any]) -> Subscription | None:
    """Locate the local subscription a Stripe object refers to.

    Tried in order of reliability: our own user id, then the Stripe subscription
    id, then the customer id.
    """
    user_id = data.get("client_reference_id") or (data.get("metadata") or {}).get("helix_user_id")
    if user_id:
        result = await db.execute(select(Subscription).where(Subscription.user_id == user_id))
        if (subscription := result.scalar_one_or_none()) is not None:
            return subscription

    subscription_id = (
        data.get("subscription") if isinstance(data.get("subscription"), str) else data.get("id")
    )
    if subscription_id:
        result = await db.execute(
            select(Subscription).where(Subscription.stripe_subscription_id == subscription_id)
        )
        if (subscription := result.scalar_one_or_none()) is not None:
            return subscription

    customer_id = data.get("customer")
    if isinstance(customer_id, str):
        result = await db.execute(select(Subscription).where(Subscription.stripe_customer_id == customer_id))
        if (subscription := result.scalar_one_or_none()) is not None:
            return subscription
    return None


async def handle_event(db: AsyncSession, event: dict[str, Any]) -> tuple[str, bool]:
    """Apply a Stripe event to local subscription state. Returns (type, handled)."""
    event_type = event.get("type", "")
    data = ((event.get("data") or {}).get("object")) or {}

    if event_type not in HANDLED_EVENTS:
        logger.info("Ignoring unhandled Stripe event %s", event_type)
        return event_type, False

    subscription = await _find_subscription(db, data)
    if subscription is None:
        logger.warning("No local subscription matched Stripe event %s", event_type)
        return event_type, False

    if event_type == "checkout.session.completed":
        subscription.tier = "pro"
        subscription.status = "active"
        if isinstance(data.get("customer"), str):
            subscription.stripe_customer_id = data["customer"]
        if isinstance(data.get("subscription"), str):
            subscription.stripe_subscription_id = data["subscription"]
        subscription.current_period_end = _timestamp(data.get("expires_at")) or (
            datetime.now(UTC) + timedelta(days=30)
        )
        subscription.cancel_at_period_end = False
        logger.info("Upgraded user %s to Pro", subscription.user_id)

    elif event_type == "customer.subscription.updated":
        status = data.get("status", "active")
        subscription.status = status
        # past_due / canceled / unpaid all mean the user is no longer entitled
        # to Pro limits, whatever the plan on the object says.
        subscription.tier = "pro" if status in ("active", "trialing") else "free"
        subscription.cancel_at_period_end = bool(data.get("cancel_at_period_end"))
        if period_end := _timestamp(data.get("current_period_end")):
            subscription.current_period_end = period_end
        if isinstance(data.get("id"), str):
            subscription.stripe_subscription_id = data["id"]

    elif event_type == "customer.subscription.deleted":
        subscription.tier = "free"
        subscription.status = "canceled"
        subscription.cancel_at_period_end = False
        logger.info("Downgraded user %s to Free", subscription.user_id)

    await db.flush()
    return event_type, True


async def complete_simulated_checkout(db: AsyncSession, user: User, session_id: str) -> Subscription:
    """Local-only shortcut standing in for Stripe's redirect + webhook."""
    if stripe_enabled():
        raise BillingError("Simulated checkout is disabled when Stripe is configured")
    subscription = await get_or_create_subscription(db, user)
    subscription.tier = "pro"
    subscription.status = "active"
    subscription.stripe_checkout_session_id = session_id
    subscription.stripe_customer_id = subscription.stripe_customer_id or f"cus_sim_{user.id[:12]}"
    subscription.stripe_subscription_id = f"sub_sim_{secrets.token_urlsafe(10)}"
    subscription.current_period_end = datetime.now(UTC) + timedelta(days=30)
    await db.flush()
    return subscription
