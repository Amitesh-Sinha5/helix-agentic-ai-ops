"""Telemetry: every LLM and agent call is measured, priced, and persisted.

`Telemetry` is created once per API request and threaded through the agent
graphs. It buffers rows in memory and writes them to `request_logs` in one flush
at the end of the request, so instrumentation costs one round trip rather than
one per node.

`TracedLLM` is the wrapper agents actually call -- it has the same surface as
`LLMClient`, but every call is timed, priced, and recorded, and can additionally
be broadcast as a live trace event to the Phase 7 WebSocket layer.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import Settings, get_settings
from app.core.llm_client import LLMClient, LLMResponse, get_llm_client

logger = logging.getLogger("helix.telemetry")


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass
class Measurement:
    """One recorded operation, mirroring a `request_logs` row."""

    operation: str
    pod: str
    latency_ms: float = 0.0
    provider: str | None = None
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    cache_hit: bool = False
    retrieval_loops: int | None = None
    classification_path: str | None = None
    faithfulness: float | None = None
    answer_relevance: float | None = None
    status: str = "ok"
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def estimate_cost(prompt_tokens: int, completion_tokens: int, settings: Settings | None = None) -> float:
    s = settings or get_settings()
    return round(
        (prompt_tokens / 1000) * s.cost_per_1k_prompt_tokens
        + (completion_tokens / 1000) * s.cost_per_1k_completion_tokens,
        8,
    )


class Telemetry:
    """Per-request measurement buffer."""

    def __init__(
        self,
        *,
        pod: str,
        request_id: str | None = None,
        user_id: str | None = None,
        endpoint: str | None = None,
        settings: Settings | None = None,
        emit: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.pod = pod
        self.request_id = request_id or new_request_id()
        self.user_id = user_id
        self.endpoint = endpoint
        self.settings = settings or get_settings()
        self.measurements: list[Measurement] = []
        # Totals must describe the whole request, not just what is still
        # buffered: `flush()` empties `measurements`, and routers read usage
        # afterwards to build the response.
        self._retired = {"cost_usd": 0.0, "tokens": 0, "llm_calls": 0, "cache_hit": False}
        self.started_at = time.perf_counter()
        # Optional sink for live trace events (Phase 7 WebSocket gateway).
        self._emit = emit

    # -- recording --------------------------------------------------------
    def record(self, measurement: Measurement) -> Measurement:
        measurement.pod = measurement.pod or self.pod
        self.measurements.append(measurement)
        return measurement

    def record_llm(self, operation: str, response: LLMResponse, **kwargs: Any) -> Measurement:
        return self.record(
            Measurement(
                operation=operation,
                pod=self.pod,
                latency_ms=response.latency_ms,
                provider=response.provider,
                model=response.model,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                cost_usd=estimate_cost(response.prompt_tokens, response.completion_tokens, self.settings),
                **kwargs,
            )
        )

    def record_cache_hit(
        self, operation: str, *, similarity: float, saved_cost_usd: float = 0.0
    ) -> Measurement:
        """A cache hit is a zero-cost, near-zero-latency row -- that contrast is
        exactly what makes the caching win visible on the observability page."""
        return self.record(
            Measurement(
                operation=operation,
                pod=self.pod,
                cache_hit=True,
                cost_usd=0.0,
                latency_ms=0.0,
                provider="cache",
                model="semantic-cache",
                extra={"similarity": round(similarity, 4), "saved_cost_usd": round(saved_cost_usd, 8)},
            )
        )

    @asynccontextmanager
    async def span(self, operation: str, **kwargs: Any) -> AsyncIterator[Measurement]:
        """Time a non-LLM step (retrieval, reranking, classification, DB work)."""
        m = Measurement(operation=operation, pod=self.pod, **kwargs)
        started = time.perf_counter()
        try:
            yield m
        except Exception as exc:
            m.status = "error"
            m.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            m.latency_ms = (time.perf_counter() - started) * 1000
            self.record(m)

    # -- live trace -------------------------------------------------------
    async def emit_event(self, event: dict[str, Any]) -> None:
        """Publish a structured trace event to the realtime layer, if attached."""
        if self._emit is None:
            return
        payload = {"request_id": self.request_id, "pod": self.pod, **event}
        try:
            result = self._emit(payload)
            if hasattr(result, "__await__"):
                await result
        except Exception:
            logger.debug("trace emit failed", exc_info=True)

    # -- rollups ----------------------------------------------------------
    @property
    def total_cost_usd(self) -> float:
        return round(self._retired["cost_usd"] + sum(m.cost_usd for m in self.measurements), 8)

    @property
    def total_tokens(self) -> int:
        return self._retired["tokens"] + sum(m.total_tokens for m in self.measurements)

    @property
    def llm_calls(self) -> int:
        return self._retired["llm_calls"] + sum(
            1 for m in self.measurements if m.provider not in (None, "cache")
        )

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started_at) * 1000

    @property
    def cache_hit(self) -> bool:
        return self._retired["cache_hit"] or any(m.cache_hit for m in self.measurements)

    def summary(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "pod": self.pod,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "llm_calls": self.llm_calls,
            "total_tokens": self.total_tokens,
            "cost_usd": self.total_cost_usd,
            "cache_hit": self.cache_hit,
            "operations": [m.operation for m in self.measurements],
        }

    def annotate_last(self, **kwargs: Any) -> None:
        if self.measurements:
            for key, value in kwargs.items():
                setattr(self.measurements[-1], key, value)

    # -- persistence ------------------------------------------------------
    async def flush(self, session: Any | None = None) -> int:
        """Write buffered measurements to `request_logs`.

        Never raises: losing telemetry must not fail a user-facing request.
        """
        if not self.measurements:
            return 0
        from app.db.models import RequestLog

        rows = [
            RequestLog(
                created_at=m.created_at,
                user_id=self.user_id,
                request_id=self.request_id,
                pod=m.pod or self.pod,
                operation=m.operation,
                endpoint=self.endpoint,
                provider=m.provider,
                model=m.model,
                latency_ms=m.latency_ms,
                prompt_tokens=m.prompt_tokens,
                completion_tokens=m.completion_tokens,
                total_tokens=m.total_tokens,
                cost_usd=m.cost_usd,
                cache_hit=m.cache_hit,
                retrieval_loops=m.retrieval_loops,
                classification_path=m.classification_path,
                faithfulness=m.faithfulness,
                answer_relevance=m.answer_relevance,
                status=m.status,
                error=m.error,
                extra=m.extra,
            )
            for m in self.measurements
        ]
        try:
            if session is not None:
                session.add_all(rows)
                await session.flush()
            else:
                from app.db.session import session_scope

                async with session_scope() as s:
                    s.add_all(rows)
            written = len(rows)
        except Exception:
            logger.warning("telemetry flush failed for request %s", self.request_id, exc_info=True)
            return 0

        # Retire the buffer into running totals before clearing it.
        self._retired["cost_usd"] += sum(m.cost_usd for m in self.measurements)
        self._retired["tokens"] += sum(m.total_tokens for m in self.measurements)
        self._retired["llm_calls"] += sum(1 for m in self.measurements if m.provider not in (None, "cache"))
        self._retired["cache_hit"] = self._retired["cache_hit"] or any(m.cache_hit for m in self.measurements)
        self.measurements.clear()
        return written


class TracedLLM:
    """`LLMClient` with telemetry and live-trace emission baked in."""

    def __init__(self, telemetry: Telemetry, client: LLMClient | None = None) -> None:
        self.telemetry = telemetry
        self.client = client or get_llm_client()

    @property
    def provider_name(self) -> str:
        return self.client.provider_name

    async def complete(self, prompt: str, *, operation: str, **kwargs: Any) -> LLMResponse:
        task = kwargs.pop("task", operation)
        try:
            resp = await self.client.complete(prompt, task=task, **kwargs)
        except Exception as exc:
            self.telemetry.record(
                Measurement(
                    operation=operation,
                    pod=self.telemetry.pod,
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise
        self.telemetry.record_llm(operation, resp)
        return resp

    async def complete_json(self, prompt: str, *, operation: str, **kwargs: Any) -> tuple[Any, LLMResponse]:
        resp = await self.complete(prompt, operation=operation, **kwargs)
        return resp.json(), resp

    async def embed(self, texts: list[str], *, operation: str = "embed") -> list[list[float]]:
        async with self.telemetry.span(operation) as m:
            vectors, tokens = await self.client.embed(texts)
            m.prompt_tokens = tokens
            m.provider = self.client.settings.embedding_provider
            m.model = self.client.settings.embedding_model
            m.cost_usd = round((tokens / 1000) * self.client.settings.cost_per_1k_embedding_tokens, 8)
            m.extra = {"count": len(texts)}
        return vectors

    async def embed_one(self, text: str, *, operation: str = "embed") -> list[float]:
        return (await self.embed([text], operation=operation))[0]
