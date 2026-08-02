"""Password hashing, JWT issuance, and refresh-token hashing."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings
from app.core.errors import AuthError

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

TokenType = Literal["access", "refresh"]


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    # bcrypt silently truncates beyond 72 bytes; reject rather than accept a
    # password whose tail is ignored.
    if len(password.encode("utf-8")) > 72:
        raise ValueError("Password must be at most 72 bytes")
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(password, hashed)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# JWTs
# --------------------------------------------------------------------------- #
def _create_token(subject: str, token_type: TokenType, expires_delta: timedelta, **claims: Any) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": secrets.token_urlsafe(16),
        **claims,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: str, *, role: str = "user", email: str | None = None) -> tuple[str, int]:
    """Returns (token, expires_in_seconds)."""
    settings = get_settings()
    ttl = timedelta(minutes=settings.access_token_expire_minutes)
    token = _create_token(user_id, "access", ttl, role=role, email=email)
    return token, int(ttl.total_seconds())


def create_refresh_token(user_id: str) -> tuple[str, datetime]:
    """Returns (token, absolute expiry). Only its hash is ever stored."""
    settings = get_settings()
    ttl = timedelta(days=settings.refresh_token_expire_days)
    token = _create_token(user_id, "refresh", ttl)
    return token, datetime.now(UTC) + ttl


def decode_token(token: str, *, expected_type: TokenType | None = None) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise AuthError(f"Invalid or expired token: {exc}") from exc
    if expected_type and payload.get("type") != expected_type:
        raise AuthError(f"Expected a {expected_type} token, got {payload.get('type')!r}")
    if not payload.get("sub"):
        raise AuthError("Token is missing a subject")
    return payload


def hash_refresh_token(token: str) -> str:
    """Refresh tokens are stored as SHA-256 digests.

    A stolen database dump then yields no usable tokens. SHA-256 (not bcrypt) is
    correct here: the token is already 128+ bits of entropy, so there is nothing
    to brute-force, and lookups stay indexable.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
