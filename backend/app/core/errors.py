"""Domain errors that map cleanly onto HTTP responses."""

from __future__ import annotations


class HelixError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, detail: str | None = None, *, code: str | None = None) -> None:
        self.detail = detail or self.__doc__ or "Internal error"
        if code:
            self.code = code
        super().__init__(self.detail)


class AuthError(HelixError):
    """Authentication failed."""

    status_code = 401
    code = "unauthorized"


class PermissionDenied(HelixError):
    """You do not have permission to perform this action."""

    status_code = 403
    code = "forbidden"


class NotFound(HelixError):
    """Resource not found."""

    status_code = 404
    code = "not_found"


class ConflictError(HelixError):
    """Resource already exists."""

    status_code = 409
    code = "conflict"


class RateLimitExceeded(HelixError):
    """Rate limit exceeded for your subscription tier."""

    status_code = 429
    code = "rate_limit_exceeded"

    def __init__(
        self,
        detail: str | None = None,
        *,
        limit: int = 0,
        used: int = 0,
        retry_after: int = 0,
        tier: str = "free",
    ) -> None:
        super().__init__(
            detail
            or (
                f"Rate limit exceeded: {used}/{limit} requests used on the {tier} tier. "
                f"Retry in {retry_after}s, or upgrade to Pro for unlimited requests."
            )
        )
        self.limit = limit
        self.used = used
        self.retry_after = retry_after
        self.tier = tier


class PayloadTooLarge(HelixError):
    """Payload exceeds the allowed size."""

    status_code = 413
    code = "payload_too_large"


class UpstreamError(HelixError):
    """An upstream provider failed."""

    status_code = 502
    code = "upstream_error"
