"""Phase 3: semantic cache and tier-aware rate limiting."""

from __future__ import annotations

import httpx

from app.config import get_settings
from app.core.cache import get_cache
from app.core.llm_client import LLMClient

# --------------------------------------------------------------------------- #
# Cache primitives
# --------------------------------------------------------------------------- #


async def test_cache_falls_back_to_in_process_when_redis_is_down():
    """The suite points REDIS_URL at a dead port on purpose."""
    cache = get_cache()
    assert await cache.ping() is False
    assert cache.backend == "memory"
    # The fallback still speaks the full protocol we rely on.
    await cache.set_json("k", {"v": 1}, ttl=60)
    assert await cache.get_json("k") == {"v": 1}


async def test_semantic_lookup_matches_a_paraphrase_but_not_a_different_question():
    cache = get_cache()
    client = LLMClient()

    stored_question = "How long is the refund window for a subscription?"
    await cache.semantic_store(
        "docqa:documents",
        stored_question,
        await client.embed_one(stored_question),
        {"answer": "30 days."},
    )

    paraphrase = "How long is the refund window for subscriptions?"
    hit = await cache.semantic_lookup(
        "docqa:documents", paraphrase, await client.embed_one(paraphrase), threshold=0.85
    )
    assert hit is not None
    assert hit.payload["answer"] == "30 days."
    assert hit.similarity >= 0.85

    unrelated = "What Kubernetes version do you run in production?"
    assert (
        await cache.semantic_lookup(
            "docqa:documents", unrelated, await client.embed_one(unrelated), threshold=0.85
        )
        is None
    )


async def test_exact_repeat_is_a_cache_hit():
    cache = get_cache()
    client = LLMClient()
    question = "What is the data retention period?"
    await cache.semantic_store("ns", question, await client.embed_one(question), {"answer": "90 days."})

    hit = await cache.semantic_lookup("ns", question, await client.embed_one(question))
    assert hit is not None and hit.exact is True and hit.similarity == 1.0


async def test_namespace_invalidation_clears_entries():
    cache = get_cache()
    client = LLMClient()
    question = "cached question about limits"
    await cache.semantic_store("ns1", question, await client.embed_one(question), {"answer": "x"})
    await cache.semantic_store("ns2", question, await client.embed_one(question), {"answer": "y"})

    removed = await cache.invalidate_namespace("ns1")
    assert removed > 0
    assert await cache.semantic_lookup("ns1", question, await client.embed_one(question)) is None
    # A different namespace is untouched.
    assert await cache.semantic_lookup("ns2", question, await client.embed_one(question)) is not None


# --------------------------------------------------------------------------- #
# Cache through the query endpoint
# --------------------------------------------------------------------------- #


async def test_repeat_question_is_served_from_cache_with_zero_llm_cost(
    user_client: httpx.AsyncClient, ingested_policy
):
    question = "How long does the free trial last?"

    first = await user_client.post("/docs/query", json={"question": question})
    assert first.status_code == 200
    assert first.headers["X-Cache"] == "MISS"
    first_body = first.json()
    assert first_body["usage"]["cost_usd"] > 0
    assert first_body["usage"]["llm_calls"] > 0

    second = await user_client.post("/docs/query", json={"question": question})
    assert second.headers["X-Cache"] == "HIT"
    second_body = second.json()

    assert second_body["answer"] == first_body["answer"]
    assert second_body["usage"]["cache_hit"] is True
    assert second_body["usage"]["llm_calls"] == 0
    assert second_body["usage"]["cost_usd"] == 0.0
    assert second_body["usage"]["latency_ms"] < first_body["usage"]["latency_ms"]
    assert second_body["trace"][0]["node"] == "semantic_cache"


async def test_paraphrased_question_hits_the_cache(user_client: httpx.AsyncClient, ingested_policy):
    """Semantic, not string, caching: near-identical wording must still hit."""
    await user_client.post("/docs/query", json={"question": "How long does the free trial last?"})
    paraphrase = await user_client.post(
        "/docs/query", json={"question": "How long does the free trial last for?"}
    )
    assert paraphrase.headers["X-Cache"] == "HIT"
    assert paraphrase.json()["usage"]["cost_usd"] == 0.0


async def test_cache_can_be_bypassed_per_request(user_client: httpx.AsyncClient, ingested_policy):
    question = "How long does the free trial last?"
    await user_client.post("/docs/query", json={"question": question})
    bypass = await user_client.post("/docs/query", json={"question": question, "use_cache": False})
    assert bypass.headers["X-Cache"] == "MISS"
    assert bypass.json()["usage"]["llm_calls"] > 0


async def test_session_queries_are_never_cache_served(user_client: httpx.AsyncClient, ingested_policy):
    """A follow-up depends on its own conversation, so it must not reuse another's."""
    question = "How long does the free trial last?"
    await user_client.post("/docs/query", json={"question": question})
    in_session = await user_client.post("/docs/query", json={"question": question, "session_id": "s-1"})
    assert in_session.headers["X-Cache"] == "MISS"


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


async def test_sliding_window_allows_then_rejects():
    cache = get_cache()
    for i in range(3):
        allowed, used, _ = await cache.sliding_window_hit("k", limit=3, window_seconds=60)
        assert allowed is True, f"request {i} should be allowed"
        assert used == i + 1

    allowed, used, retry_after = await cache.sliding_window_hit("k", limit=3, window_seconds=60)
    assert allowed is False
    assert used == 3
    assert retry_after > 0


async def test_rejected_request_does_not_extend_its_own_window():
    """A blocked request must not count as a hit, or the window never drains."""
    cache = get_cache()
    for _ in range(2):
        await cache.sliding_window_hit("k2", limit=2, window_seconds=60)
    for _ in range(5):
        await cache.sliding_window_hit("k2", limit=2, window_seconds=60)

    used, _ = await cache.sliding_window_usage("k2", window_seconds=60)
    assert used == 2


async def test_free_tier_is_limited_and_returns_429(user_client: httpx.AsyncClient, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "free_tier_daily_requests", 3)

    for _ in range(3):
        response = await user_client.post("/code-review/analyze", json={"code": "x = 1"})
        assert response.status_code == 201
        assert response.headers["X-RateLimit-Tier"] == "free"

    blocked = await user_client.post("/code-review/analyze", json={"code": "x = 1"})
    assert blocked.status_code == 429
    body = blocked.json()
    assert body["code"] == "rate_limit_exceeded"
    assert body["limit"] == 3
    assert "upgrade" in body["detail"].lower()
    assert int(blocked.headers["Retry-After"]) > 0


async def test_unmetered_endpoints_stay_open_when_rate_limited(user_client: httpx.AsyncClient, monkeypatch):
    """A throttled user must still be able to check usage and upgrade."""
    settings = get_settings()
    monkeypatch.setattr(settings, "free_tier_daily_requests", 1)

    await user_client.post("/code-review/analyze", json={"code": "x = 1"})
    assert (await user_client.post("/code-review/analyze", json={"code": "y = 2"})).status_code == 429

    assert (await user_client.get("/billing/usage")).status_code == 200
    assert (await user_client.get("/auth/me")).status_code == 200
    assert (await user_client.post("/billing/checkout", json={"tier": "pro"})).status_code == 200


async def test_pro_tier_is_unlimited(user_client: httpx.AsyncClient, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "free_tier_daily_requests", 1)

    checkout = await user_client.post("/billing/checkout", json={"tier": "pro"})
    session_id = checkout.json()["session_id"]
    upgraded = await user_client.post(f"/billing/simulate-completion?session_id={session_id}")
    assert upgraded.json()["tier"] == "pro"

    # The limit lifts immediately -- no restart, no cache warm-up.
    for _ in range(4):
        response = await user_client.post("/code-review/analyze", json={"code": "x = 1"})
        assert response.status_code == 201
        assert response.headers["X-RateLimit-Limit"] == "unlimited"
        assert response.headers["X-RateLimit-Tier"] == "pro"


async def test_rate_limits_are_isolated_per_user(client: httpx.AsyncClient, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "free_tier_daily_requests", 1)

    tokens = []
    for email in ("a@helix.example.com", "b@helix.example.com"):
        signup = await client.post("/auth/signup", json={"email": email, "password": "passw0rd1"})
        tokens.append(signup.json()["tokens"]["access_token"])

    for token in tokens:
        headers = {"Authorization": f"Bearer {token}"}
        assert (
            await client.post("/code-review/analyze", json={"code": "x = 1"}, headers=headers)
        ).status_code == 201

    # Each user burned their own single request, not a shared one.
    for token in tokens:
        headers = {"Authorization": f"Bearer {token}"}
        assert (
            await client.post("/code-review/analyze", json={"code": "x = 1"}, headers=headers)
        ).status_code == 429


async def test_usage_endpoint_reflects_the_limiter_counter(user_client: httpx.AsyncClient, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "free_tier_daily_requests", 10)

    await user_client.post("/code-review/analyze", json={"code": "x = 1"})
    await user_client.post("/code-review/analyze", json={"code": "y = 2"})

    usage = (await user_client.get("/billing/usage")).json()
    assert usage["tier"] == "free"
    assert usage["used"] == 2
    assert usage["limit"] == 10
    assert usage["remaining"] == 8
    assert usage["unlimited"] is False
