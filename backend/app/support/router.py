"""Support Triage endpoints."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import func, select

from app.config import get_settings
from app.core.deps import CurrentUser, DBSession, RequireAdmin, make_telemetry
from app.core.errors import NotFound
from app.core.telemetry import Telemetry, TracedLLM
from app.db.models import Ticket
from app.realtime.gateway import publish_escalation
from app.schemas.common import EscalationEvent, Page, TraceEvent, UsageStats
from app.schemas.support import KBSource, TicketOut, TriageRequest, TriageResponse
from app.support.agents import SupportTriagePipeline
from app.support.classifier import get_classifier

logger = logging.getLogger("helix.support.router")

router = APIRouter(prefix="/support", tags=["support-triage"])

PodTelemetry = Annotated[Telemetry, Depends(make_telemetry("support_triage"))]


@router.post("/triage", response_model=TriageResponse, status_code=status.HTTP_201_CREATED)
async def triage_ticket(
    payload: TriageRequest, user: CurrentUser, db: DBSession, telemetry: PodTelemetry, response: Response
) -> TriageResponse:
    """Classify, draft a reply, and decide escalation for one ticket."""
    response.headers["X-Request-ID"] = telemetry.request_id

    result = await SupportTriagePipeline(TracedLLM(telemetry), telemetry).run(
        payload.subject, payload.body, collection=payload.collection, owner_id=user.id
    )

    ticket = Ticket(
        user_id=user.id,
        request_id=telemetry.request_id,
        subject=payload.subject,
        body=payload.body,
        customer_email=payload.customer_email,
        priority=result["priority"],
        category=result["category"],
        confidence=result["confidence"],
        classification_path=result["classification_path"],
        draft_response=result["draft_response"],
        escalate=result["escalate"],
        escalation_reason=result["escalation_reason"],
        suggested_owner=result["suggested_owner"],
        status="escalated" if result["escalate"] else "triaged",
        kb_sources=result["kb_sources"],
    )
    db.add(ticket)
    await db.flush()

    if result["escalate"]:
        # Fire-and-forget onto Redis; admins connected to /ws/admin/escalations
        # see this within about a second, with no polling.
        await publish_escalation(
            EscalationEvent(
                ticket_id=ticket.id,
                request_id=telemetry.request_id,
                subject=payload.subject,
                priority=result["priority"],
                category=result["category"],
                reason=result["escalation_reason"] or "Escalation requested",
                suggested_owner=result["suggested_owner"],
                customer_email=payload.customer_email,
            ).model_dump(mode="json")
        )

    await telemetry.flush(db)

    return TriageResponse(
        request_id=telemetry.request_id,
        ticket_id=ticket.id,
        subject=payload.subject,
        priority=result["priority"],
        category=result["category"],
        confidence=result["confidence"],
        classification_path=result["classification_path"],
        draft_response=result["draft_response"],
        escalate=result["escalate"],
        escalation_reason=result["escalation_reason"],
        suggested_owner=result["suggested_owner"],
        kb_sources=[KBSource(**s) for s in result["kb_sources"]],
        trace=[TraceEvent(**e) for e in result["trace"]],
        usage=UsageStats(
            request_id=telemetry.request_id,
            latency_ms=round(telemetry.elapsed_ms, 2),
            llm_calls=telemetry.llm_calls,
            total_tokens=telemetry.total_tokens,
            cost_usd=telemetry.total_cost_usd,
        ),
    )


@router.get("/tickets", response_model=Page[TicketOut])
async def list_tickets(
    user: CurrentUser,
    db: DBSession,
    limit: int = 25,
    offset: int = 0,
    escalated_only: bool = False,
) -> Page[TicketOut]:
    conditions = [Ticket.user_id == user.id]
    if escalated_only:
        conditions.append(Ticket.escalate.is_(True))

    total = await db.scalar(select(func.count()).select_from(Ticket).where(*conditions)) or 0
    result = await db.execute(
        select(Ticket).where(*conditions).order_by(Ticket.created_at.desc()).limit(limit).offset(offset)
    )
    return Page[TicketOut](
        items=[TicketOut.model_validate(t) for t in result.scalars().all()],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: str, user: CurrentUser, db: DBSession) -> dict:
    ticket = await db.get(Ticket, ticket_id)
    if ticket is None or (ticket.user_id != user.id and not user.is_admin):
        raise NotFound(f"Ticket {ticket_id} not found")
    return {
        "id": ticket.id,
        "subject": ticket.subject,
        "body": ticket.body,
        "priority": ticket.priority,
        "category": ticket.category,
        "confidence": ticket.confidence,
        "classification_path": ticket.classification_path,
        "draft_response": ticket.draft_response,
        "escalate": ticket.escalate,
        "escalation_reason": ticket.escalation_reason,
        "suggested_owner": ticket.suggested_owner,
        "kb_sources": ticket.kb_sources,
        "status": ticket.status,
        "created_at": ticket.created_at,
    }


@router.get("/classifier/info")
async def classifier_info(admin: RequireAdmin) -> dict:
    """Expose the trained model's held-out metrics for the observability page."""
    classifier = get_classifier()
    artefact = classifier.load()
    if artefact is None:
        return {
            "available": False,
            "detail": "No trained artefact. Run: python -m scripts.train_classifier",
        }
    return {
        "available": True,
        "metrics": artefact.get("metrics", {}),
        "dataset_size": artefact.get("dataset_size"),
        "priority_labels": artefact.get("priority_labels", []),
        "category_labels": artefact.get("category_labels", []),
        "confidence_threshold": get_settings().classifier_confidence_threshold,
    }
