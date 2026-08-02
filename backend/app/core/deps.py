"""Shared FastAPI dependencies: current user, role guards, telemetry, tiering."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthError, PermissionDenied
from app.core.security import decode_token
from app.core.telemetry import Telemetry, new_request_id
from app.db.models import Subscription, User
from app.db.session import get_db

bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")

DBSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: DBSession,
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthError("Missing bearer token")
    payload = decode_token(credentials.credentials, expected_type="access")
    user = await db.get(User, payload["sub"])
    if user is None:
        raise AuthError("Token subject no longer exists")
    if not user.is_active:
        raise AuthError("Account is disabled")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_optional_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: DBSession,
) -> User | None:
    """Same as `get_current_user` but tolerates anonymous callers."""
    if credentials is None or not credentials.credentials:
        return None
    try:
        payload = decode_token(credentials.credentials, expected_type="access")
    except AuthError:
        return None
    return await db.get(User, payload["sub"])


def require_role(*roles: str) -> Callable[[User], User]:
    """Dependency factory enforcing role membership.

    `admin` is a superset of `user`, so an admin passes any role check.
    """

    allowed = set(roles)

    async def _guard(user: CurrentUser) -> User:
        if user.role not in allowed and user.role != "admin":
            raise PermissionDenied(
                f"This endpoint requires one of: {', '.join(sorted(allowed))}. Your role is '{user.role}'."
            )
        return user

    return _guard


RequireAdmin = Annotated[User, Depends(require_role("admin"))]


async def get_user_tier(db: AsyncSession, user_id: str) -> str:
    """Resolve a user's billing tier, defaulting to free."""
    result = await db.execute(select(Subscription).where(Subscription.user_id == user_id))
    sub = result.scalar_one_or_none()
    if sub is None or sub.status not in ("active", "trialing"):
        return "free"
    return sub.tier


def make_telemetry(pod: str) -> Callable[..., Telemetry]:
    """Dependency factory producing a per-request Telemetry bound to the caller.

    The request id is echoed back in the `X-Request-ID` response header and used
    as the WebSocket channel name for the live trace.
    """

    async def _factory(request: Request, user: CurrentUser) -> Telemetry:
        from app.realtime.gateway import trace_hub

        request_id = request.headers.get("X-Request-ID") or new_request_id()
        telemetry = Telemetry(
            pod=pod,
            request_id=request_id,
            user_id=user.id,
            endpoint=request.url.path,
            emit=trace_hub.publish,
        )
        request.state.request_id = request_id
        return telemetry

    return _factory
