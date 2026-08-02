"""Phases 8 + 9: Stripe billing lifecycle and the observability read model."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from sqlalchemy import select

from app.billing import service
from app.config import get_settings
from app.db.session import session_scope


def stripe_event(event_type: str, obj: dict) -> dict:
    """A Stripe webhook envelope, shaped like the real thing."""
    return {
        "id": f"evt_test_{abs(hash(event_type)) % 10**8}",
        "object": "event",
        "type": event_type,
        "created": int(time.time()),
        "data": {"object": obj},
    }


async def _user_id(email: str) -> str:
    from app.db.models import User

    async with session_scope() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalar_one().id


# --------------------------------------------------------------------------- #
# Checkout
# --------------------------------------------------------------------------- #


async def test_checkout_returns_a_redirect_url(user_client: httpx.AsyncClient):
    response = await user_client.post("/billing/checkout", json={"tier": "pro"})
    assert response.status_code == 200
    body = response.json()

    assert body["checkout_url"].startswith("http")
    assert body["session_id"].startswith("cs_test_")
    assert body["mode"] == "simulated", "no Stripe key configured in tests"


async def test_new_user_starts_on_the_free_tier(user_client: httpx.AsyncClient):
    body = (await user_client.get("/billing/subscription")).json()
    assert body["tier"] == "free"
    assert body["status"] == "active"


async def test_checkout_requires_authentication(client: httpx.AsyncClient):
    assert (await client.post("/billing/checkout", json={"tier": "pro"})).status_code == 401


# --------------------------------------------------------------------------- #
# Webhook lifecycle
# --------------------------------------------------------------------------- #


async def test_checkout_completed_upgrades_the_user(user_client: httpx.AsyncClient):
    user_id = await _user_id("user@helix.example.com")

    response = await user_client.post(
        "/billing/webhook",
        content=json.dumps(
            stripe_event(
                "checkout.session.completed",
                {
                    "id": "cs_test_123",
                    "client_reference_id": user_id,
                    "customer": "cus_test_1",
                    "subscription": "sub_test_1",
                    "metadata": {"helix_user_id": user_id, "tier": "pro"},
                },
            )
        ),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {"received": True, "event_type": "checkout.session.completed", "handled": True}

    subscription = (await user_client.get("/billing/subscription")).json()
    assert subscription["tier"] == "pro"
    assert subscription["status"] == "active"
    assert subscription["stripe_customer_id"] == "cus_test_1"


async def test_subscription_deleted_downgrades_to_free(user_client: httpx.AsyncClient):
    user_id = await _user_id("user@helix.example.com")
    await user_client.post(
        "/billing/webhook",
        content=json.dumps(
            stripe_event(
                "checkout.session.completed",
                {"id": "cs_1", "client_reference_id": user_id, "customer": "cus_1", "subscription": "sub_1"},
            )
        ),
    )
    assert (await user_client.get("/billing/subscription")).json()["tier"] == "pro"

    await user_client.post(
        "/billing/webhook",
        content=json.dumps(
            stripe_event("customer.subscription.deleted", {"id": "sub_1", "customer": "cus_1"})
        ),
    )
    body = (await user_client.get("/billing/subscription")).json()
    assert body["tier"] == "free"
    assert body["status"] == "canceled"


@pytest.mark.parametrize(
    "status,expected_tier",
    [("active", "pro"), ("trialing", "pro"), ("past_due", "free"), ("unpaid", "free"), ("canceled", "free")],
)
async def test_subscription_updated_maps_status_to_entitlement(
    user_client: httpx.AsyncClient, status: str, expected_tier: str
):
    """A past_due subscription must lose Pro limits even though a plan exists."""
    user_id = await _user_id("user@helix.example.com")
    await user_client.post(
        "/billing/webhook",
        content=json.dumps(
            stripe_event(
                "checkout.session.completed",
                {"id": "cs_1", "client_reference_id": user_id, "customer": "cus_1", "subscription": "sub_1"},
            )
        ),
    )

    await user_client.post(
        "/billing/webhook",
        content=json.dumps(
            stripe_event(
                "customer.subscription.updated",
                {
                    "id": "sub_1",
                    "customer": "cus_1",
                    "status": status,
                    "cancel_at_period_end": False,
                    "current_period_end": int(time.time()) + 86400,
                },
            )
        ),
    )
    assert (await user_client.get("/billing/subscription")).json()["tier"] == expected_tier


async def test_unhandled_event_is_acknowledged_but_not_applied(user_client: httpx.AsyncClient):
    response = await user_client.post(
        "/billing/webhook", content=json.dumps(stripe_event("invoice.paid", {"id": "in_1"}))
    )
    assert response.status_code == 200
    assert response.json()["handled"] is False


async def test_webhook_for_an_unknown_user_is_not_applied(user_client: httpx.AsyncClient):
    response = await user_client.post(
        "/billing/webhook",
        content=json.dumps(
            stripe_event("checkout.session.completed", {"id": "cs_x", "client_reference_id": "nobody"})
        ),
    )
    assert response.json()["handled"] is False


async def test_malformed_webhook_payload_is_rejected(user_client: httpx.AsyncClient):
    response = await user_client.post("/billing/webhook", content=b"not json at all")
    assert response.status_code == 400


async def test_webhook_signature_is_verified_when_a_secret_is_set(
    user_client: httpx.AsyncClient, monkeypatch
):
    """The webhook is unauthenticated, so the signature is the authentication."""
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test_secret")
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_dummy")

    response = await user_client.post(
        "/billing/webhook",
        content=json.dumps(stripe_event("checkout.session.completed", {"id": "cs_1"})),
        headers={"Stripe-Signature": "t=1,v1=deadbeef"},
    )
    assert response.status_code == 400
    assert "signature" in response.json()["detail"].lower()


async def test_upgrade_lifts_the_rate_limit_immediately(user_client: httpx.AsyncClient, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "free_tier_daily_requests", 1)
    user_id = await _user_id("user@helix.example.com")

    assert (await user_client.post("/code-review/analyze", json={"code": "x=1"})).status_code == 201
    assert (await user_client.post("/code-review/analyze", json={"code": "x=1"})).status_code == 429

    await user_client.post(
        "/billing/webhook",
        content=json.dumps(
            stripe_event(
                "checkout.session.completed",
                {"id": "cs_1", "client_reference_id": user_id, "customer": "cus_1", "subscription": "sub_1"},
            )
        ),
    )

    assert (await user_client.post("/code-review/analyze", json={"code": "x=1"})).status_code == 201
    assert (await user_client.get("/billing/usage")).json()["unlimited"] is True


def test_handled_events_are_the_three_that_matter():
    assert set(service.HANDLED_EVENTS) == {
        "checkout.session.completed",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }


# --------------------------------------------------------------------------- #
# Observability
# --------------------------------------------------------------------------- #


async def test_summary_aggregates_across_pods(admin_client: httpx.AsyncClient):
    await admin_client.post(
        "/docs/ingest", json={"title": "Policy", "text": "The free trial lasts 14 days from signup."}
    )
    await admin_client.post("/docs/query", json={"question": "How long is the free trial?"})
    await admin_client.post("/code-review/analyze", json={"code": "x = eval(user_input)"})
    await admin_client.post("/support/triage", json={"subject": "Refund", "body": "I was charged twice."})

    summary = (await admin_client.get("/observability/summary")).json()

    assert summary["total_requests"] > 0
    assert summary["total_llm_calls"] > 0
    assert summary["total_tokens"] > 0
    assert summary["total_cost_usd"] > 0
    assert summary["avg_latency_ms"] >= 0
    assert summary["p95_latency_ms"] >= 0

    pods = {p["pod"] for p in summary["pods"]}
    assert {"doc_qa", "code_review", "support_triage"} <= pods

    for pod in summary["pods"]:
        assert pod["requests"] > 0
        assert 0.0 <= pod["error_rate"] <= 1.0
        assert 0.0 <= pod["cache_hit_rate"] <= 1.0


async def test_summary_reports_cache_savings(admin_client: httpx.AsyncClient):
    await admin_client.post(
        "/docs/ingest", json={"title": "Policy", "text": "The free trial lasts 14 days from signup."}
    )
    question = {"question": "How long is the free trial?"}
    await admin_client.post("/docs/query", json=question)
    await admin_client.post("/docs/query", json=question)  # served from cache

    summary = (await admin_client.get("/observability/summary")).json()
    doc_qa = next(p for p in summary["pods"] if p["pod"] == "doc_qa")

    assert doc_qa["cache_hits"] >= 1
    assert doc_qa["cache_hit_rate"] > 0
    assert doc_qa["estimated_cost_saved_usd"] > 0
    assert summary["estimated_cost_saved_usd"] > 0


async def test_summary_reports_the_support_classification_split(admin_client: httpx.AsyncClient):
    await admin_client.post(
        "/support/triage",
        json={"subject": "Refund", "body": "I was charged twice for my subscription, please refund."},
    )
    summary = (await admin_client.get("/observability/summary")).json()
    support = next(p for p in summary["pods"] if p["pod"] == "support_triage")

    assert support["trained_model_share"] is not None
    assert 0.0 <= support["trained_model_share"] <= 1.0


async def test_summary_reports_retrieval_loops(admin_client: httpx.AsyncClient):
    await admin_client.post(
        "/docs/ingest", json={"title": "Policy", "text": "The Free plan allows 20 agent requests per day."}
    )
    await admin_client.post("/docs/query", json={"question": "What are the caps?"})

    summary = (await admin_client.get("/observability/summary")).json()
    doc_qa = next(p for p in summary["pods"] if p["pod"] == "doc_qa")
    assert doc_qa["avg_retrieval_loops"] is not None
    assert doc_qa["avg_retrieval_loops"] >= 1


async def test_recent_requests_drilldown(admin_client: httpx.AsyncClient):
    await admin_client.post("/code-review/analyze", json={"code": "x = 1"})
    body = (await admin_client.get("/observability/requests?pod=code_review")).json()

    assert body["total"] > 0
    row = body["items"][0]
    assert row["pod"] == "code_review"
    assert row["operation"].startswith("review.")
    assert row["status"] == "ok"


async def test_empty_window_returns_zeroed_summary(admin_client: httpx.AsyncClient):
    summary = (await admin_client.get("/observability/summary")).json()
    assert summary["total_requests"] == 0
    assert summary["total_cost_usd"] == 0
    assert summary["pods"] == []


async def test_summary_is_admin_only(user_client: httpx.AsyncClient):
    assert (await user_client.get("/observability/summary")).status_code == 403
    assert (await user_client.get("/observability/requests")).status_code == 403
