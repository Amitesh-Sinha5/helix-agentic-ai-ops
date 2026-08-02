"""Per-user, tier-aware sliding-window rate limiting.

Only the metered agent endpoints are limited -- auth, billing, health and the
observability read model stay open so a rate-limited user can still log in, see
their usage, and upgrade. The tier is read from the `subscriptions` table
(written by the Phase 9 Stripe webhook), so upgrading to Pro lifts the limit on
the very next request with no restart.
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from app.config import get_settings
from app.core.cache import get_cache
from app.core.errors import AuthError
from app.core.security import decode_token

logger = logging.getLogger("helix.ratelimit")

METERED_PREFIXES: tuple[str, ...] = (
    "/docs/query",
    "/docs/ingest",
    "/code-review/analyze",
    "/support/triage",
)


def rate_limit_key(user_id: str) -> str:
    return f"helix:ratelimit:{user_id}"


async def resolve_tier(user_id: str) -> str:
    from sqlalchemy import select

    from app.db.models import Subscription
    from app.db.session import session_scope

    try:
        async with session_scope() as session:
            result = await session.execute(select(Subscription).where(Subscription.user_id == user_id))
            sub = result.scalar_one_or_none()
    except Exception:
        logger.debug("tier lookup failed for %s", user_id, exc_info=True)
        return "free"
    if sub is None or sub.status not in ("active", "trialing"):
        return "free"
    return sub.tier


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self.settings = get_settings()

    def _is_metered(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in METERED_PREFIXES)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        settings = get_settings()
        if not settings.rate_limit_enabled or not self._is_metered(request.url.path):
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            # Unauthenticated: let the route's auth dependency produce the 401.
            return await call_next(request)
        try:
            claims = decode_token(auth_header.split(" ", 1)[1].strip(), expected_type="access")
        except AuthError:
            return await call_next(request)

        user_id = claims["sub"]
        tier = await resolve_tier(user_id)
        limit = settings.tier_limit(tier)

        if limit < 0:  # unlimited
            response = await call_next(request)
            response.headers["X-RateLimit-Limit"] = "unlimited"
            response.headers["X-RateLimit-Tier"] = tier
            return response

        cache = get_cache()
        allowed, used, retry_after = await cache.sliding_window_hit(
            rate_limit_key(user_id), limit=limit, window_seconds=settings.rate_limit_window_seconds
        )
        if not allowed:
            logger.info("rate limit hit user=%s tier=%s used=%s", user_id, tier, used)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": (
                        f"Rate limit exceeded: {used}/{limit} requests used on the {tier} tier. "
                        f"Retry in {retry_after}s, or upgrade to Pro for unlimited requests."
                    ),
                    "code": "rate_limit_exceeded",
                    "limit": limit,
                    "used": used,
                    "tier": tier,
                    "retry_after": retry_after,
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Tier": tier,
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limit - used))
        response.headers["X-RateLimit-Tier"] = tier
        return response
