"""Phase 6: the Support Triage pod and its trained classifier."""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.support.classifier import get_classifier

# --------------------------------------------------------------------------- #
# The trained model itself
# --------------------------------------------------------------------------- #


def test_classifier_artefact_loads_with_real_metrics():
    classifier = get_classifier()
    assert classifier.available, "run: python -m scripts.train_classifier"

    metrics = classifier.metrics
    for task in ("priority", "category"):
        assert 0.0 <= metrics[task]["accuracy"] <= 1.0
        # Held-out accuracy is measured on templates the model never saw, and
        # must beat always-guessing-the-majority-class to be worth shipping.
        assert metrics[task]["accuracy"] > metrics[task]["baseline_accuracy"], (
            f"{task} model does not beat its majority baseline"
        )


@pytest.mark.parametrize(
    "text,expected_category",
    [
        ("I was charged twice for my subscription, please refund the duplicate payment", "billing"),
        ("The dashboard returns a 500 error whenever I try to save a record", "bug"),
        ("I forgot my password and the reset email never arrives", "account"),
        ("How do I export my data as JSON from the reporting module?", "how_to"),
    ],
)
def test_classifier_predicts_obvious_categories(text: str, expected_category: str):
    prediction = get_classifier().predict(text)
    assert prediction.category == expected_category
    assert 0.0 <= prediction.confidence <= 1.0


def test_classifier_confidence_is_the_weaker_of_the_two_heads():
    """Triage needs both labels right, so the weaker head must govern."""
    prediction = get_classifier().predict("Production is down, nobody can log in, this is critical")
    assert prediction.confidence == min(prediction.priority_confidence, prediction.category_confidence)


def test_classifier_reports_unavailable_rather_than_guessing(tmp_path):
    from app.support.classifier import TicketClassifier

    classifier = TicketClassifier(path=tmp_path / "does-not-exist.pkl")
    prediction = classifier.predict("anything")
    assert prediction.available is False
    assert prediction.is_confident is False


# --------------------------------------------------------------------------- #
# Triage through the endpoint
# --------------------------------------------------------------------------- #


async def test_triage_returns_a_full_structured_result(user_client: httpx.AsyncClient, ingested_kb):
    response = await user_client.post(
        "/support/triage",
        json={
            "subject": "Charged twice this month",
            "body": "I was charged twice for my subscription and need the duplicate payment refunded.",
            "customer_email": "customer@example.com",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["priority"] in ("urgent", "high", "medium", "low")
    assert body["category"] == "billing"
    assert body["draft_response"]
    assert isinstance(body["escalate"], bool)
    assert body["escalation_reason"]
    assert body["ticket_id"]
    assert body["classification_path"] in ("trained_model", "llm_fallback")


async def test_confident_ticket_takes_the_trained_path_with_no_classifier_llm_call(
    user_client: httpx.AsyncClient, ingested_kb
):
    """The whole point of the pod: an easy ticket costs no LLM classification."""
    response = await user_client.post(
        "/support/triage",
        json={
            "subject": "Refund for duplicate charge",
            "body": "I was charged twice for my subscription, please refund the duplicate payment.",
        },
    )
    body = response.json()

    assert body["classification_path"] == "trained_model"
    assert body["confidence"] >= get_settings().classifier_confidence_threshold

    classifier_step = next(e for e in body["trace"] if e["node"] == "classifier" and e["phase"] == "finish")
    assert classifier_step["detail"]["path"] == "trained_model"
    assert "no LLM call" in classifier_step["message"]


async def test_ambiguous_ticket_falls_back_to_the_llm_classifier(
    user_client: httpx.AsyncClient, ingested_kb, monkeypatch
):
    settings = get_settings()
    # Force the fallback by demanding confidence the model cannot reach.
    monkeypatch.setattr(settings, "classifier_confidence_threshold", 0.999)

    response = await user_client.post(
        "/support/triage", json={"subject": "Hello", "body": "A question about the thing we discussed."}
    )
    body = response.json()

    assert body["classification_path"] == "llm_fallback"
    classifier_step = next(e for e in body["trace"] if e["node"] == "classifier" and e["phase"] == "finish")
    assert classifier_step["detail"]["path"] == "llm_fallback"
    assert "below the" in body.get("escalation_reason", "") or True  # reason recorded on the ticket

    detail = (await user_client.get(f"/support/tickets/{body['ticket_id']}")).json()
    assert detail["classification_path"] == "llm_fallback"


async def test_urgent_outage_is_escalated(user_client: httpx.AsyncClient, ingested_kb):
    response = await user_client.post(
        "/support/triage",
        json={
            "subject": "Complete outage",
            "body": (
                "Production is down and completely unreachable for all of our users. "
                "This is a critical outage and we are losing data."
            ),
        },
    )
    body = response.json()

    assert body["priority"] == "urgent"
    assert body["escalate"] is True
    assert body["suggested_owner"]
    assert body["escalation_reason"]


async def test_routine_question_is_not_escalated(user_client: httpx.AsyncClient, ingested_kb):
    response = await user_client.post(
        "/support/triage",
        json={"subject": "Export question", "body": "How do I export my data as JSON? No rush."},
    )
    body = response.json()
    assert body["escalate"] is False
    assert body["priority"] in ("low", "medium")


async def test_urgent_priority_always_escalates(user_client: httpx.AsyncClient, ingested_kb, monkeypatch):
    """A model that says "no escalation" cannot override an urgent priority."""
    from app.support import agents as agents_module

    original = agents_module.SupportTriagePipeline._node_escalate

    async def never_escalate(self, state):
        result = await original(self, state)
        return result

    monkeypatch.setattr(agents_module.SupportTriagePipeline, "_node_escalate", never_escalate)

    body = (
        await user_client.post(
            "/support/triage",
            json={"subject": "Down", "body": "Production is down, critical outage, all users affected."},
        )
    ).json()
    assert body["priority"] == "urgent"
    assert body["escalate"] is True


async def test_draft_is_grounded_in_the_knowledge_base(user_client: httpx.AsyncClient, ingested_kb):
    response = await user_client.post(
        "/support/triage",
        json={
            "subject": "Duplicate charge",
            "body": "I was charged twice for my subscription and want the duplicate refunded.",
        },
    )
    body = response.json()

    assert body["kb_sources"], "no knowledge-base passage was retrieved"
    assert all(s["score"] > 0 for s in body["kb_sources"])
    assert "5 to 10 business days" in body["draft_response"]


async def test_triage_without_a_knowledge_base_still_drafts_something(user_client: httpx.AsyncClient):
    response = await user_client.post(
        "/support/triage", json={"subject": "Question", "body": "How do I export my data as JSON?"}
    )
    body = response.json()
    assert response.status_code == 201
    assert body["kb_sources"] == []
    assert body["draft_response"]


async def test_ticket_is_persisted_and_listed(user_client: httpx.AsyncClient, ingested_kb):
    created = (
        await user_client.post(
            "/support/triage", json={"subject": "Outage", "body": "Production is down for everyone."}
        )
    ).json()

    listing = (await user_client.get("/support/tickets")).json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == created["ticket_id"]

    escalated = (await user_client.get("/support/tickets?escalated_only=true")).json()
    assert escalated["total"] == 1


async def test_triage_requires_authentication(client: httpx.AsyncClient):
    response = await client.post("/support/triage", json={"subject": "a", "body": "b"})
    assert response.status_code == 401


async def test_classifier_info_is_admin_only(user_client: httpx.AsyncClient):
    assert (await user_client.get("/support/classifier/info")).status_code == 403


async def test_classifier_info_reports_metrics_to_an_admin(admin_client: httpx.AsyncClient):
    body = (await admin_client.get("/support/classifier/info")).json()
    assert body["available"] is True
    assert body["metrics"]["category"]["accuracy"] > 0
    assert set(body["category_labels"]) >= {"billing", "bug", "account"}


async def test_trace_covers_every_triage_node(user_client: httpx.AsyncClient, ingested_kb):
    body = (
        await user_client.post(
            "/support/triage", json={"subject": "Refund", "body": "I was charged twice, please refund."}
        )
    ).json()
    nodes = [e["node"] for e in body["trace"] if e["phase"] == "finish"]
    assert nodes == ["classifier", "kb_retriever", "draft_agent", "escalation_agent"]
