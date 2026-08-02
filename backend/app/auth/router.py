"""Authentication: signup, login, refresh-token rotation, logout."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Request, status
from sqlalchemy import select, update

from app.core.deps import CurrentUser, DBSession
from app.core.errors import AuthError, ConflictError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.db.models import RefreshToken, Subscription, User
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    SignupRequest,
    TokenPair,
    UserOut,
)

logger = logging.getLogger("helix.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


async def _issue_tokens(db: DBSession, user: User, *, user_agent: str | None = None) -> TokenPair:
    access, expires_in = create_access_token(user.id, role=user.role, email=user.email)
    refresh, expires_at = create_refresh_token(user.id)
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(refresh),
            expires_at=expires_at,
            user_agent=(user_agent or "")[:300] or None,
        )
    )
    await db.flush()
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=expires_in)


async def _revoke_family(db: DBSession, user_id: str) -> None:
    """Revoke every live refresh token for a user (theft response / logout-all)."""
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, request: Request, db: DBSession) -> AuthResponse:
    """Create an account and return a token pair.

    The first account created on an empty instance becomes the admin, so a fresh
    deployment is administrable without a manual DB edit.
    """
    email = payload.email.lower().strip()
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise ConflictError("An account with that email already exists")

    user_count = (await db.execute(select(User.id))).scalars().all()
    user = User(
        email=email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        role="admin" if not user_count else "user",
    )
    db.add(user)
    await db.flush()

    # Every user starts on an explicit free subscription so the rate limiter and
    # billing page never have to reason about a missing row.
    db.add(Subscription(user_id=user.id, tier="free", status="active"))

    tokens = await _issue_tokens(db, user, user_agent=request.headers.get("user-agent"))
    logger.info("signup user=%s role=%s", user.id, user.role)
    return AuthResponse(user=UserOut.model_validate(user), tokens=tokens)


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginRequest, request: Request, db: DBSession) -> AuthResponse:
    result = await db.execute(select(User).where(User.email == payload.email.lower().strip()))
    user = result.scalar_one_or_none()
    # Identical error for unknown-email and wrong-password: no account enumeration.
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise AuthError("Incorrect email or password")
    if not user.is_active:
        raise AuthError("Account is disabled")

    tokens = await _issue_tokens(db, user, user_agent=request.headers.get("user-agent"))
    return AuthResponse(user=UserOut.model_validate(user), tokens=tokens)


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, request: Request, db: DBSession) -> TokenPair:
    """Rotate a refresh token.

    Rotation is single-use. Presenting an already-rotated token means the token
    leaked, so the entire family is revoked and the holder must log in again.
    """
    claims = decode_token(payload.refresh_token, expected_type="refresh")
    token_hash = hash_refresh_token(payload.refresh_token)

    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    stored = result.scalar_one_or_none()
    if stored is None:
        raise AuthError("Refresh token not recognised")

    if stored.revoked_at is not None:
        # `replaced_by` is what separates the two ways a token can be dead.
        # Set => it was rotated, and someone is replaying a token that should
        # have been discarded: treat that as theft and kill the whole family.
        # Unset => it was explicitly logged out, which is just a stale client
        # retrying; reject this one token and leave other sessions alone.
        if stored.replaced_by is not None:
            logger.warning("refresh token reuse detected for user=%s; revoking family", stored.user_id)
            await _revoke_family(db, stored.user_id)
            # Commit before raising: the request-scoped session rolls back on an
            # exception, which would silently undo the revocation we just made.
            await db.commit()
            raise AuthError("Refresh token has already been used; all sessions revoked")
        raise AuthError("Refresh token has been revoked")

    expires_at = stored.expires_at
    if expires_at.tzinfo is None:  # SQLite hands back naive datetimes
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        raise AuthError("Refresh token has expired")

    user = await db.get(User, claims["sub"])
    if user is None or not user.is_active:
        raise AuthError("Account is unavailable")

    tokens = await _issue_tokens(db, user, user_agent=request.headers.get("user-agent"))
    stored.revoked_at = datetime.now(UTC)
    stored.replaced_by = hash_refresh_token(tokens.refresh_token)
    return tokens


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(payload: LogoutRequest, user: CurrentUser, db: DBSession) -> dict:
    if payload.all_sessions:
        await _revoke_family(db, user.id)
        return {"revoked": "all"}

    if not payload.refresh_token:
        raise AuthError("Provide a refresh_token, or set all_sessions=true")

    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_refresh_token(payload.refresh_token),
            RefreshToken.user_id == user.id,
        )
    )
    stored = result.scalar_one_or_none()
    if stored is None:
        raise AuthError("Refresh token not recognised")
    stored.revoked_at = datetime.now(UTC)
    return {"revoked": "session"}


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
