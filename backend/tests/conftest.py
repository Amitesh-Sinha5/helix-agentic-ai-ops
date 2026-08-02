"""Shared test fixtures.

Every test runs fully offline: the mock LLM provider, a per-test SQLite file, an
in-process fake Redis, and an ephemeral Chroma client. No API keys, no network,
no docker.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

# Environment must be set before app.config is imported anywhere.
os.environ.update(
    {
        "ENVIRONMENT": "ci",
        "LLM_PROVIDER": "mock",
        "EMBEDDING_PROVIDER": "mock",
        "REDIS_URL": "redis://127.0.0.1:6399/15",  # unreachable on purpose -> fake fallback
        "REDIS_REQUIRED": "false",
        "JWT_SECRET_KEY": "test-secret-key-for-pytest-only",
        "RATE_LIMIT_ENABLED": "true",
        "SEMANTIC_CACHE_ENABLED": "true",
        "STRIPE_SECRET_KEY": "",
        "STRIPE_WEBHOOK_SECRET": "",
    }
)

import httpx  # noqa: E402
from httpx import ASGITransport  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.cache import reset_cache_state  # noqa: E402
from app.core.llm_client import reset_llm_client  # noqa: E402
from app.db.session import create_all, dispose_engine, drop_all  # noqa: E402
from app.rag.retrieval import invalidate_keyword_index  # noqa: E402
from app.rag.vectorstore import clear_chroma_system_cache, get_vector_store, reset_vector_store  # noqa: E402
from app.support.classifier import reset_classifier  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "quality_gate: RAG quality metrics enforced in CI")
    # The classifier artefact is a build product, not source: train it on demand
    # so a fresh clone can run the suite with no extra step.
    artefact = BACKEND_ROOT / "app" / "support" / "classifier.pkl"
    if not artefact.exists():
        from scripts.train_classifier import train

        train(artefact, quiet=True)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
async def isolated_environment(tmp_path, monkeypatch) -> AsyncIterator[None]:
    """Give every test its own database, cache, and vector store."""
    db_path = tmp_path / "helix_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    # A per-test Chroma directory, not an ephemeral client: Chroma shares one
    # System between clients constructed with identical settings, so ephemeral
    # clients leak documents from one test into the next.
    monkeypatch.setenv("CHROMA_PERSIST_DIR", str(tmp_path / "chroma"))

    get_settings.cache_clear()
    reset_llm_client()
    reset_vector_store()
    clear_chroma_system_cache()
    reset_classifier()
    invalidate_keyword_index()
    await dispose_engine()
    await reset_cache_state()

    await create_all()
    try:
        yield
    finally:
        await drop_all()
        await dispose_engine()
        await reset_cache_state()
        invalidate_keyword_index()
        reset_vector_store()
        clear_chroma_system_cache()
        get_settings.cache_clear()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client bound to the ASGI app (no live server, real routing)."""
    from app.main import app

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture
async def user_client(client: httpx.AsyncClient) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-up, authenticated *non-admin* client.

    The first account on a fresh instance becomes admin, so a throwaway admin is
    created first and this fixture returns the second account.
    """
    await client.post("/auth/signup", json={"email": "admin@helix.example.com", "password": "adminpass123"})
    response = await client.post(
        "/auth/signup",
        json={"email": "user@helix.example.com", "password": "userpass123", "full_name": "Test User"},
    )
    assert response.status_code == 201, response.text
    token = response.json()["tokens"]["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    yield client


@pytest.fixture
async def admin_client(client: httpx.AsyncClient) -> AsyncIterator[httpx.AsyncClient]:
    """An authenticated admin client (the first account created)."""
    response = await client.post(
        "/auth/signup", json={"email": "root@helix.example.com", "password": "rootpass123"}
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["user"]["role"] == "admin"
    client.headers["Authorization"] = f"Bearer {payload['tokens']['access_token']}"
    yield client


# --------------------------------------------------------------------------- #
# Corpus fixtures
# --------------------------------------------------------------------------- #

SAMPLE_POLICY_DOC = """
Helix Subscription and Refund Policy

Refunds and the trial period.
Every new Helix workspace begins with a free trial that lasts 14 days from the
date of signup. No credit card is required to start the trial. Customers may
request a full refund within 30 days of their first payment, and refunds are
issued back to the original payment method within 5 to 10 business days.

Plan limits.
The Free plan allows 20 agent requests per day per user. The Pro plan removes
the daily request cap entirely and adds priority support. Enterprise customers
receive a dedicated support engineer and a 99.9 percent uptime guarantee.

Data retention.
Documents uploaded to Helix are retained for 90 days after deletion in cold
storage before being permanently destroyed. Audit logs are retained for 12
months. Customers on the Enterprise plan may configure a custom retention
period of between 30 days and 7 years.

Security and compliance.
Helix is SOC 2 Type II certified. All data is encrypted at rest with AES-256 and
in transit with TLS 1.3. Access to production systems requires hardware-backed
two factor authentication.
"""

SAMPLE_KB_DOC = """
Helix Support Knowledge Base

Password resets.
If a customer cannot sign in, direct them to the Forgot Password link on the
login page. Reset emails are delivered within 5 minutes; if none arrives, check
the spam folder and confirm the address matches the account on file.

Duplicate charges.
When a customer reports being charged twice, verify the transaction in the
billing portal. Confirmed duplicate charges are refunded in full within 5 to 10
business days, and no further action is required from the customer.

Service outages.
For a reported outage, confirm the status page first. Outages affecting more
than one customer are escalated immediately to the on-call engineer, and
customers should be told that updates will follow every 30 minutes.

Export and data requests.
Customers can export their data from Settings, then Export, in either JSON or
CSV format. Large exports are prepared asynchronously and emailed as a download
link that expires after 24 hours.
"""


@pytest.fixture
async def ingested_policy(user_client: httpx.AsyncClient) -> dict:
    response = await user_client.post(
        "/docs/ingest",
        json={
            "title": "Subscription and Refund Policy",
            "text": SAMPLE_POLICY_DOC,
            "source": "policy.md",
            "collection": "documents",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def ingested_kb(user_client: httpx.AsyncClient) -> dict:
    response = await user_client.post(
        "/docs/ingest",
        json={
            "title": "Support Knowledge Base",
            "text": SAMPLE_KB_DOC,
            "source": "kb.md",
            "collection": "knowledge_base",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def vector_store():
    return get_vector_store()
