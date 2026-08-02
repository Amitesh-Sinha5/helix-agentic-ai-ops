"""Helix API entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.core.cache import close_cache, get_cache
from app.core.errors import HelixError
from app.core.rate_limit import RateLimitMiddleware
from app.db.session import dispose_engine, get_engine
from app.schemas.common import HealthResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("helix")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Starting %s (env=%s, llm_provider=%s)",
        settings.app_name,
        settings.environment,
        settings.llm_provider,
    )
    # Warm the support classifier so the first triage request is not slow.
    from app.support.classifier import get_classifier

    get_classifier().load()
    yield
    await close_cache()
    await dispose_engine()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Helix API",
    description=(
        "Enterprise agentic AI operations platform: three agent pods (Doc Q&A, "
        "Code Review, Support Triage) over one governed backend."
    ),
    version="1.0.0",
    lifespan=lifespan,
    # The Doc Q&A pod owns /docs/*, so the interactive API docs move aside.
    docs_url="/api-docs",
    redoc_url="/api-redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)


@app.exception_handler(HelixError)
async def helix_error_handler(request: Request, exc: HelixError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Pydantic puts the raw exception object in each error's `ctx`, which json
    # cannot serialise -- jsonable_encoder is what makes the payload safe.
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Request validation failed",
            "code": "validation_error",
            "errors": jsonable_encoder(exc.errors()),
        },
    )


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    """Liveness probe. Reports dependency status without failing on their absence."""
    db_status = "unknown"
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as exc:
        db_status = f"error: {type(exc).__name__}"

    cache = get_cache()
    redis_status = "ok" if await cache.ping() else f"fallback:{cache.backend}"

    return HealthResponse(
        status="ok",
        llm_provider=settings.llm_provider,
        database=db_status,
        redis=redis_status,
    )


# --------------------------------------------------------------------------- #
# Routers
# --------------------------------------------------------------------------- #
from app.auth.router import router as auth_router  # noqa: E402
from app.billing.router import router as billing_router  # noqa: E402
from app.code_review.router import router as code_review_router  # noqa: E402
from app.observability.router import router as observability_router  # noqa: E402
from app.rag.router import router as rag_router  # noqa: E402
from app.realtime.gateway import router as realtime_router  # noqa: E402
from app.support.router import router as support_router  # noqa: E402

app.include_router(auth_router)
app.include_router(rag_router)
app.include_router(code_review_router)
app.include_router(support_router)
app.include_router(billing_router)
app.include_router(observability_router)
app.include_router(realtime_router)
