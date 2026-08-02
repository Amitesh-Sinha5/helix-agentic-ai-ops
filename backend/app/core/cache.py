"""Redis client wrapper, semantic answer cache, and pub/sub.

Connection strategy: try the configured Redis; if it is unreachable and
`REDIS_REQUIRED` is false, transparently fall back to an in-process fake that
speaks the same protocol. Tests and a bare `uvicorn` run therefore exercise the
identical code path -- there is no "if redis else dict" branching sprinkled
through the codebase.

The semantic cache is the interesting part: instead of keying on an exact string
match, it embeds the question and returns a stored answer when cosine similarity
to a previous question clears `SEMANTIC_CACHE_THRESHOLD`. "What is the refund
window?" therefore hits the entry stored for "How long do I have to get a
refund?" -- a real LLM call avoided, which shows up as zero cost in telemetry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.core.llm_client import cosine

logger = logging.getLogger("helix.cache")

ESCALATIONS_CHANNEL = "escalations"
TRACE_CHANNEL_PREFIX = "trace:"


@dataclass
class SemanticHit:
    payload: dict[str, Any]
    similarity: float
    cached_question: str
    exact: bool = False


class Cache:
    """Async Redis wrapper with an automatic in-process fallback."""

    def __init__(self, url: str | None = None) -> None:
        settings = get_settings()
        self.url = url or settings.redis_url
        self.settings = settings
        self._client: Any | None = None
        self._lock = asyncio.Lock()
        self.backend = "uninitialised"

    # -- connection -------------------------------------------------------
    async def client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is not None:
                return self._client
            self._client = await self._connect()
        return self._client

    async def _connect(self) -> Any:
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(
                self.url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=1.5,
                socket_timeout=3.0,
                health_check_interval=30,
            )
            await asyncio.wait_for(client.ping(), timeout=2.0)
            self.backend = "redis"
            logger.info("Connected to Redis at %s", self.url)
            return client
        except Exception as exc:
            if self.settings.redis_required:
                raise
            logger.warning("Redis unavailable (%s); using in-process fallback", exc)
            import fakeredis.aioredis as fakeredis_aio

            self.backend = "memory"
            return fakeredis_aio.FakeRedis(decode_responses=True, server=_fake_server())

    async def ping(self) -> bool:
        try:
            client = await self.client()
            await client.ping()
        except Exception:
            return False
        return self.backend == "redis"

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                logger.debug("cache close failed", exc_info=True)
            self._client = None
            self.backend = "uninitialised"

    # -- primitives -------------------------------------------------------
    async def get_json(self, key: str) -> Any | None:
        raw = await (await self.client()).get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        payload = json.dumps(value, default=str)
        client = await self.client()
        if ttl:
            await client.setex(key, ttl, payload)
        else:
            await client.set(key, payload)

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return int(await (await self.client()).delete(*keys))

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every key under a prefix using SCAN (never KEYS, which blocks)."""
        client = await self.client()
        removed, cursor = 0, 0
        while True:
            cursor, keys = await client.scan(cursor=cursor, match=f"{prefix}*", count=500)
            if keys:
                removed += int(await client.delete(*keys))
            if cursor == 0:
                return removed

    async def incr(self, key: str, ttl: int | None = None) -> int:
        client = await self.client()
        value = int(await client.incr(key))
        if ttl and value == 1:
            await client.expire(key, ttl)
        return value

    # -- pub/sub ----------------------------------------------------------
    async def publish(self, channel: str, message: Any) -> int:
        client = await self.client()
        return int(await client.publish(channel, json.dumps(message, default=str)))

    async def subscribe(self, channel: str) -> Any:
        """Return a subscribed PubSub object. Caller is responsible for closing."""
        client = await self.client()
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)
        return pubsub

    # -- sliding-window rate limiting -------------------------------------
    async def sliding_window_hit(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int, int]:
        """Record a hit and report (allowed, used, retry_after_seconds).

        Implemented as a sorted set of request timestamps: drop everything older
        than the window, count what is left, and add the new hit only if the
        request is allowed -- a rejected request must not extend its own window.
        """
        client = await self.client()
        now = time.time()
        cutoff = now - window_seconds

        pipe = client.pipeline()
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.zcard(key)
        results = await pipe.execute()
        used = int(results[-1])

        if limit >= 0 and used >= limit:
            oldest = await client.zrange(key, 0, 0, withscores=True)
            retry_after = int(max(1, (oldest[0][1] + window_seconds) - now)) if oldest else window_seconds
            return False, used, retry_after

        pipe = client.pipeline()
        pipe.zadd(key, {f"{now}:{id(object())}": now})
        pipe.expire(key, window_seconds)
        await pipe.execute()
        return True, used + 1, 0

    async def sliding_window_usage(self, key: str, *, window_seconds: int) -> tuple[int, int]:
        """Read-only usage: (used, seconds_until_oldest_hit_expires)."""
        client = await self.client()
        now = time.time()
        await client.zremrangebyscore(key, 0, now - window_seconds)
        used = int(await client.zcard(key))
        oldest = await client.zrange(key, 0, 0, withscores=True)
        resets_in = int(max(0, (oldest[0][1] + window_seconds) - now)) if oldest else window_seconds
        return used, resets_in

    # -- semantic cache ---------------------------------------------------
    @staticmethod
    def _ns_key(namespace: str) -> str:
        return f"helix:semcache:{namespace}"

    @staticmethod
    def _exact_key(namespace: str, question: str) -> str:
        digest = hashlib.sha256(question.strip().lower().encode()).hexdigest()[:32]
        return f"helix:semcache:{namespace}:exact:{digest}"

    async def semantic_lookup(
        self,
        namespace: str,
        question: str,
        embedding: list[float],
        *,
        threshold: float | None = None,
    ) -> SemanticHit | None:
        if not self.settings.semantic_cache_enabled:
            return None
        threshold = self.settings.semantic_cache_threshold if threshold is None else threshold

        # Exact match first: one O(1) GET avoids scanning the entry set at all.
        exact = await self.get_json(self._exact_key(namespace, question))
        if exact is not None:
            return SemanticHit(payload=exact, similarity=1.0, cached_question=question, exact=True)

        client = await self.client()
        entries = await client.hgetall(self._ns_key(namespace))
        best: SemanticHit | None = None
        stale: list[str] = []
        now = time.time()
        for field, raw in entries.items():
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                stale.append(field)
                continue
            if entry.get("expires_at", 0) < now:
                stale.append(field)
                continue
            similarity = cosine(embedding, entry.get("embedding", []))
            if similarity >= threshold and (best is None or similarity > best.similarity):
                best = SemanticHit(
                    payload=entry.get("payload", {}),
                    similarity=similarity,
                    cached_question=entry.get("question", ""),
                )
        if stale:
            await client.hdel(self._ns_key(namespace), *stale)
        return best

    async def semantic_store(
        self,
        namespace: str,
        question: str,
        embedding: list[float],
        payload: dict[str, Any],
        *,
        ttl: int | None = None,
    ) -> None:
        if not self.settings.semantic_cache_enabled:
            return
        ttl = ttl or self.settings.semantic_cache_ttl_seconds
        client = await self.client()
        key = self._ns_key(namespace)
        entry = {
            "question": question,
            "embedding": embedding,
            "payload": payload,
            "expires_at": time.time() + ttl,
        }
        field = hashlib.sha256(question.strip().lower().encode()).hexdigest()[:32]
        await client.hset(key, field, json.dumps(entry, default=str))
        await client.expire(key, ttl * 2)
        await self.set_json(self._exact_key(namespace, question), payload, ttl=ttl)

        # Bound the namespace so the scan above stays cheap: evict soonest-expiring.
        if await client.hlen(key) > self.settings.semantic_cache_max_entries:
            entries = await client.hgetall(key)
            by_expiry = sorted(
                entries.items(),
                key=lambda kv: json.loads(kv[1]).get("expires_at", 0) if kv[1] else 0,
            )
            overflow = len(entries) - self.settings.semantic_cache_max_entries
            await client.hdel(key, *[f for f, _ in by_expiry[:overflow]])

    async def invalidate_namespace(self, namespace: str) -> int:
        """Drop a whole cache namespace -- called when a document is re-ingested,
        because previously cached answers may now be stale."""
        client = await self.client()
        removed = int(await client.delete(self._ns_key(namespace)))
        removed += await self.delete_prefix(f"helix:semcache:{namespace}:exact:")
        return removed


_fake_server_singleton: Any = None


def _fake_server():
    """One shared fake server so every Cache instance sees the same data."""
    global _fake_server_singleton
    if _fake_server_singleton is None:
        import fakeredis

        _fake_server_singleton = fakeredis.FakeServer()
    return _fake_server_singleton


_cache: Cache | None = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache()
    return _cache


async def close_cache() -> None:
    global _cache
    if _cache is not None:
        await _cache.close()
    _cache = None


async def reset_cache_state() -> None:
    """Test hook: flush all cached data and drop the client."""
    global _cache, _fake_server_singleton
    if _cache is not None:
        try:
            client = await _cache.client()
            await client.flushdb()
        except Exception:
            pass
        await _cache.close()
    _cache = None
    _fake_server_singleton = None
