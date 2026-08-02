"""Phase 7: WebSocket trace streaming and admin escalation alerts."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.cache import ESCALATIONS_CHANNEL, get_cache
from app.realtime.gateway import TraceHub, publish_escalation, trace_hub


async def test_trace_events_are_buffered_and_replayed():
    """A socket that connects mid-request must still receive the earlier steps."""
    await trace_hub.publish(
        {
            "request_id": "req-1",
            "pod": "doc_qa",
            "node": "retriever",
            "phase": "finish",
            "sequence": 1,
            "duration_ms": 12.5,
            "detail": {"vector_hits": 4, "keyword_hits": 3, "kept": 3},
            "message": "hybrid search complete",
        }
    )
    replayed = await trace_hub.replay("req-1")

    assert len(replayed) == 1
    event = replayed[0]
    assert event["node"] == "retriever"
    assert event["detail"]["vector_hits"] == 4
    assert isinstance(event["detail"], dict), "trace detail must be structured, not a string"


async def test_events_are_isolated_per_request():
    await trace_hub.publish({"request_id": "a", "node": "n1", "phase": "finish"})
    await trace_hub.publish({"request_id": "b", "node": "n2", "phase": "finish"})

    assert [e["node"] for e in await trace_hub.replay("a")] == ["n1"]
    assert [e["node"] for e in await trace_hub.replay("b")] == ["n2"]


async def test_publish_without_a_request_id_is_a_no_op():
    await trace_hub.publish({"node": "orphan"})
    assert await trace_hub.replay("") == []


async def test_escalation_is_published_to_the_channel():
    cache = get_cache()
    pubsub = await cache.subscribe(ESCALATIONS_CHANNEL)
    await asyncio.sleep(0.05)

    await publish_escalation({"ticket_id": "t-1", "subject": "Outage", "priority": "urgent"})

    received = None
    for _ in range(50):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if message and message.get("data"):
            import json

            received = json.loads(message["data"])
            break
        await asyncio.sleep(0.02)
    await pubsub.aclose()

    assert received is not None, "escalation never arrived on the channel"
    assert received["ticket_id"] == "t-1"
    assert received["priority"] == "urgent"


async def test_query_emits_structured_trace_events_to_the_hub(
    user_client: httpx.AsyncClient, ingested_policy
):
    """End to end: a real query must leave a replayable structured trace."""
    response = await user_client.post(
        "/docs/query",
        headers={"X-Request-ID": "trace-e2e"},
        json={"question": "How long does the free trial last?", "use_cache": False},
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "trace-e2e"

    events = await trace_hub.replay("trace-e2e")
    assert events, "no trace events were published"

    nodes = {e["node"] for e in events}
    assert {"retriever", "context_check", "answer", "validator"} <= nodes

    retriever = next(e for e in events if e["node"] == "retriever" and e["phase"] == "finish")
    for field in ("vector_hits", "keyword_hits", "fused_hits", "kept", "reranker", "top_score"):
        assert field in retriever["detail"], f"{field} missing from the retriever trace"
    assert retriever["duration_ms"] >= 0
    assert isinstance(retriever["sequence"], int)


# --------------------------------------------------------------------------- #
# Live sockets
# --------------------------------------------------------------------------- #


def _token(client: TestClient, email: str) -> tuple[str, str]:
    response = client.post("/auth/signup", json={"email": email, "password": "passw0rd1"})
    body = response.json()
    return body["tokens"]["access_token"], body["user"]["role"]


def test_trace_socket_rejects_a_missing_or_bad_token():
    from app.main import app

    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/ws/agent-status/req-x"),
        ):
            pass
        assert excinfo.value.code == 4401

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws/agent-status/req-x?token=not-a-jwt"),
        ):
            pass


def test_trace_socket_replays_a_completed_request():
    """A socket opened *after* the request finished still gets the full trace.

    This is the race the replay buffer exists for: the browser fires the query
    and opens the socket at nearly the same moment.
    """
    from app.main import app

    with TestClient(app) as client:
        token, _ = _token(client, "ws-user@helix.example.com")
        client.headers["Authorization"] = f"Bearer {token}"

        client.post(
            "/docs/ingest",
            json={"title": "Policy", "text": "The free trial lasts 14 days from signup, no card required."},
        )
        client.post(
            "/docs/query",
            headers={"X-Request-ID": "replayed-req"},
            json={"question": "How long is the free trial?", "use_cache": False},
        )

        with client.websocket_connect(f"/ws/agent-status/replayed-req?token={token}") as socket:
            assert socket.receive_json() == {"type": "connected", "request_id": "replayed-req"}

            nodes = []
            while True:
                message = socket.receive_json()
                if message["type"] == "replay_complete":
                    assert message["count"] == len(nodes)
                    break
                assert message["type"] == "trace"
                nodes.append(message["event"]["node"])

            assert "retriever" in nodes
            assert "answer" in nodes
            assert "validator" in nodes


def test_escalation_socket_requires_an_admin():
    from app.main import app

    with TestClient(app) as client:
        _token(client, "first-admin@helix.example.com")  # first account becomes admin
        user_token, role = _token(client, "plain-user@helix.example.com")
        assert role == "user"

        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect(f"/ws/admin/escalations?token={user_token}"),
        ):
            pass
        assert excinfo.value.code == 4403


def test_admin_receives_a_live_escalation():
    from app.main import app

    with TestClient(app) as client:
        admin_token, role = _token(client, "ws-admin@helix.example.com")
        assert role == "admin"
        client.headers["Authorization"] = f"Bearer {admin_token}"

        with client.websocket_connect(f"/ws/admin/escalations?token={admin_token}") as socket:
            assert socket.receive_json() == {"type": "connected", "role": "admin"}

            client.post(
                "/docs/ingest",
                json={"title": "KB", "text": "Outages are escalated to the on-call engineer immediately."},
            )
            response = client.post(
                "/support/triage",
                json={
                    "subject": "Total outage",
                    "body": "Production is down and unreachable for all users, this is a critical outage.",
                },
            )
            assert response.status_code == 201
            assert response.json()["escalate"] is True

            message = socket.receive_json()
            assert message["type"] == "escalation"
            event = message["event"]
            assert event["ticket_id"] == response.json()["ticket_id"]
            assert event["priority"] == "urgent"
            assert event["reason"]


def test_channel_naming_is_stable():
    assert TraceHub.channel("abc") == "trace:abc"
    assert TraceHub.buffer_key("abc") == "helix:trace:abc:buffer"
