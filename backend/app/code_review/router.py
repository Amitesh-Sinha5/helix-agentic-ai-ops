"""Code Review endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import func, select

from app.code_review.agents import CodeReviewPipeline
from app.core.deps import CurrentUser, DBSession, make_telemetry
from app.core.errors import NotFound
from app.core.telemetry import Telemetry, TracedLLM
from app.db.models import CodeReview
from app.schemas.code_review import CodeReviewOut, CodeReviewRequest, CodeReviewResult
from app.schemas.common import Page, TraceEvent, UsageStats

router = APIRouter(prefix="/code-review", tags=["code-review"])

PodTelemetry = Annotated[Telemetry, Depends(make_telemetry("code_review"))]


@router.post("/analyze", response_model=CodeReviewResult, status_code=status.HTTP_201_CREATED)
async def analyze(
    payload: CodeReviewRequest, user: CurrentUser, db: DBSession, telemetry: PodTelemetry, response: Response
) -> CodeReviewResult:
    """Run the parallel quality/security review and persist the structured result."""
    response.headers["X-Request-ID"] = telemetry.request_id

    result = await CodeReviewPipeline(TracedLLM(telemetry), telemetry).run(
        payload.code, language=payload.language, filename=payload.filename, context=payload.context
    )

    record = CodeReview(
        user_id=user.id,
        request_id=telemetry.request_id,
        filename=payload.filename,
        language=payload.language,
        code=payload.code,
        verdict=result["verdict"].value,
        summary=result["summary"],
        issues=[i.model_dump(mode="json") for i in result["issues"]],
        issue_count=result["issue_count"],
        blocking_count=result["blocking_count"],
    )
    db.add(record)
    await db.flush()
    await telemetry.flush(db)

    return CodeReviewResult(
        request_id=telemetry.request_id,
        review_id=record.id,
        filename=payload.filename,
        language=payload.language,
        verdict=result["verdict"],
        summary=result["summary"],
        issues=result["issues"],
        issue_count=result["issue_count"],
        blocking_count=result["blocking_count"],
        severity_counts=result["severity_counts"],
        top_recommendation=result["top_recommendation"],
        trace=[TraceEvent(**e) for e in result["trace"]],
        usage=UsageStats(
            request_id=telemetry.request_id,
            latency_ms=round(telemetry.elapsed_ms, 2),
            llm_calls=telemetry.llm_calls,
            total_tokens=telemetry.total_tokens,
            cost_usd=telemetry.total_cost_usd,
        ),
    )


@router.get("/reviews", response_model=Page[CodeReviewOut])
async def list_reviews(
    user: CurrentUser, db: DBSession, limit: int = 25, offset: int = 0
) -> Page[CodeReviewOut]:
    total = (
        await db.scalar(select(func.count()).select_from(CodeReview).where(CodeReview.user_id == user.id))
        or 0
    )
    result = await db.execute(
        select(CodeReview)
        .where(CodeReview.user_id == user.id)
        .order_by(CodeReview.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return Page[CodeReviewOut](
        items=[CodeReviewOut.model_validate(r) for r in result.scalars().all()],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get("/reviews/{review_id}")
async def get_review(review_id: str, user: CurrentUser, db: DBSession) -> dict:
    record = await db.get(CodeReview, review_id)
    if record is None or record.user_id != user.id:
        raise NotFound(f"Review {review_id} not found")
    return {
        "id": record.id,
        "filename": record.filename,
        "language": record.language,
        "verdict": record.verdict,
        "summary": record.summary,
        "issues": record.issues,
        "issue_count": record.issue_count,
        "blocking_count": record.blocking_count,
        "created_at": record.created_at,
    }
