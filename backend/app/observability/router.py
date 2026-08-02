"""Observability: aggregated cost, latency, cache and quality metrics.

Admin-only, per the Phase 2 RBAC guard. Everything here is derived from the
`request_logs` rows written by `core.telemetry`, so the numbers are measured
rather than estimated -- including the money the semantic cache saved, which is
computed from what the avoided calls actually cost when they last ran.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query
from sqlalchemy import case, func, select

from app.core.deps import DBSession, RequireAdmin
from app.db.models import RequestLog
from app.schemas.observability import ObservabilitySummary, PodStats

router = APIRouter(prefix="/observability", tags=["observability"])


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Computed in Python because SQLite has no
    percentile function and the row counts here are small."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered) + 0.5) - 1))
    return round(ordered[index], 2)


@router.get("/summary", response_model=ObservabilitySummary)
async def summary(
    admin: RequireAdmin,
    db: DBSession,
    window_hours: int = Query(default=24, ge=1, le=24 * 90),
) -> ObservabilitySummary:
    """Aggregate stats per pod over a rolling window."""
    since = datetime.now(UTC) - timedelta(hours=window_hours)

    rows = (
        (
            await db.execute(
                select(
                    RequestLog.pod,
                    func.count(RequestLog.id).label("operations"),
                    func.count(func.distinct(RequestLog.request_id)).label("requests"),
                    func.sum(RequestLog.total_tokens).label("tokens"),
                    func.sum(RequestLog.cost_usd).label("cost"),
                    func.avg(RequestLog.latency_ms).label("avg_latency"),
                    func.sum(case((RequestLog.cache_hit.is_(True), 1), else_=0)).label("cache_hits"),
                    func.sum(case((RequestLog.status == "error", 1), else_=0)).label("errors"),
                    func.sum(case((RequestLog.provider.notin_(("cache",)), 1), else_=0)).label("llm_calls"),
                    func.avg(RequestLog.retrieval_loops).label("avg_loops"),
                    func.avg(RequestLog.faithfulness).label("avg_faithfulness"),
                    func.avg(RequestLog.answer_relevance).label("avg_relevance"),
                    func.sum(case((RequestLog.classification_path == "trained_model", 1), else_=0)).label(
                        "trained_path"
                    ),
                    func.sum(case((RequestLog.classification_path.isnot(None), 1), else_=0)).label(
                        "classified"
                    ),
                )
                .where(RequestLog.created_at >= since)
                .group_by(RequestLog.pod)
            )
        )
        .mappings()
        .all()
    )

    latency_rows = (
        await db.execute(select(RequestLog.pod, RequestLog.latency_ms).where(RequestLog.created_at >= since))
    ).all()
    latencies_by_pod: dict[str, list[float]] = {}
    for pod, latency in latency_rows:
        latencies_by_pod.setdefault(pod, []).append(float(latency or 0.0))

    # What the cache saved: every hit records the cost of the call it replaced.
    saved_rows = (
        await db.execute(
            select(RequestLog.pod, RequestLog.extra).where(
                RequestLog.created_at >= since, RequestLog.cache_hit.is_(True)
            )
        )
    ).all()
    saved_by_pod: dict[str, float] = {}
    for pod, extra in saved_rows:
        saved_by_pod[pod] = saved_by_pod.get(pod, 0.0) + float(
            (extra or {}).get("saved_cost_usd", 0.0) or 0.0
        )

    pods: list[PodStats] = []
    for row in rows:
        operations = int(row["operations"] or 0)
        cache_hits = int(row["cache_hits"] or 0)
        pods.append(
            PodStats(
                pod=row["pod"],
                requests=int(row["requests"] or 0),
                llm_calls=int(row["llm_calls"] or 0),
                total_tokens=int(row["tokens"] or 0),
                total_cost_usd=round(float(row["cost"] or 0.0), 6),
                avg_latency_ms=round(float(row["avg_latency"] or 0.0), 2),
                p95_latency_ms=_percentile(latencies_by_pod.get(row["pod"], []), 95),
                error_rate=round(int(row["errors"] or 0) / operations, 4) if operations else 0.0,
                cache_hits=cache_hits,
                cache_hit_rate=round(cache_hits / operations, 4) if operations else 0.0,
                estimated_cost_saved_usd=round(saved_by_pod.get(row["pod"], 0.0), 6),
                avg_retrieval_loops=(
                    round(float(row["avg_loops"]), 3) if row["avg_loops"] is not None else None
                ),
                avg_faithfulness=(
                    round(float(row["avg_faithfulness"]), 4) if row["avg_faithfulness"] is not None else None
                ),
                avg_answer_relevance=(
                    round(float(row["avg_relevance"]), 4) if row["avg_relevance"] is not None else None
                ),
                trained_model_share=(
                    round(int(row["trained_path"] or 0) / int(row["classified"]), 4)
                    if int(row["classified"] or 0)
                    else None
                ),
            )
        )

    total_ops = sum(int(r["operations"] or 0) for r in rows)
    total_cache_hits = sum(int(r["cache_hits"] or 0) for r in rows)
    total_errors = sum(int(r["errors"] or 0) for r in rows)
    all_latencies = [latency for values in latencies_by_pod.values() for latency in values]

    faithfulness_values = [p.avg_faithfulness for p in pods if p.avg_faithfulness is not None]
    relevance_values = [p.avg_answer_relevance for p in pods if p.avg_answer_relevance is not None]

    top_operations = (
        (
            await db.execute(
                select(
                    RequestLog.operation,
                    func.count(RequestLog.id).label("calls"),
                    func.sum(RequestLog.cost_usd).label("cost"),
                    func.avg(RequestLog.latency_ms).label("avg_latency"),
                )
                .where(RequestLog.created_at >= since)
                .group_by(RequestLog.operation)
                .order_by(func.count(RequestLog.id).desc())
                .limit(10)
            )
        )
        .mappings()
        .all()
    )

    return ObservabilitySummary(
        window_hours=window_hours,
        generated_at=datetime.now(UTC).isoformat(),
        total_requests=sum(p.requests for p in pods),
        total_llm_calls=sum(p.llm_calls for p in pods),
        total_tokens=sum(p.total_tokens for p in pods),
        total_cost_usd=round(sum(p.total_cost_usd for p in pods), 6),
        estimated_cost_saved_usd=round(sum(p.estimated_cost_saved_usd for p in pods), 6),
        overall_cache_hit_rate=round(total_cache_hits / total_ops, 4) if total_ops else 0.0,
        avg_latency_ms=round(sum(all_latencies) / len(all_latencies), 2) if all_latencies else 0.0,
        p95_latency_ms=_percentile(all_latencies, 95),
        error_rate=round(total_errors / total_ops, 4) if total_ops else 0.0,
        avg_faithfulness=(
            round(sum(faithfulness_values) / len(faithfulness_values), 4) if faithfulness_values else None
        ),
        avg_answer_relevance=(
            round(sum(relevance_values) / len(relevance_values), 4) if relevance_values else None
        ),
        pods=sorted(pods, key=lambda p: p.total_cost_usd, reverse=True),
        top_operations=[
            {
                "operation": row["operation"],
                "calls": int(row["calls"]),
                "cost_usd": round(float(row["cost"] or 0.0), 6),
                "avg_latency_ms": round(float(row["avg_latency"] or 0.0), 2),
            }
            for row in top_operations
        ],
    )


@router.get("/requests")
async def recent_requests(
    admin: RequireAdmin, db: DBSession, limit: int = Query(default=50, ge=1, le=500), pod: str | None = None
) -> dict:
    """Recent operation log, newest first -- the drill-down behind the summary."""
    query = select(RequestLog).order_by(RequestLog.created_at.desc()).limit(limit)
    if pod:
        query = query.where(RequestLog.pod == pod)
    rows = (await db.execute(query)).scalars().all()
    return {
        "items": [
            {
                "request_id": r.request_id,
                "pod": r.pod,
                "operation": r.operation,
                "provider": r.provider,
                "model": r.model,
                "latency_ms": round(r.latency_ms, 2),
                "total_tokens": r.total_tokens,
                "cost_usd": r.cost_usd,
                "cache_hit": r.cache_hit,
                "retrieval_loops": r.retrieval_loops,
                "classification_path": r.classification_path,
                "status": r.status,
                "error": r.error,
                "created_at": r.created_at,
            }
            for r in rows
        ],
        "total": len(rows),
    }
