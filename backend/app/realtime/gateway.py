"""WebSocket gateway: live agent traces and admin escalation alerts.

Two endpoints:

* `/ws/agent-status/{request_id}` -- structured trace events from every agent
  node, so the UI can render an expandable reasoning trace instead of a spinner.
* `/ws/admin/escalations` -- admin-only feed of support tickets the triage pod
  decided need a human, pushed the moment they happen.

Everything goes through Redis pub/sub rather than an in-process set of sockets,
so a client connected to one uvicorn worker still sees events produced by
another. A short replay buffer covers the race where a request starts before its
socket finishes connecting: late subscribers get the events they missed.

Browsers cannot set headers on a WebSocket handshake, so the JWT arrives as a
query parameter and is validated before the connection is accepted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core.cache import ESCALATIONS_CHANNEL, TRACE_CHANNEL_PREFIX, get_cache
from app.core.errors import AuthError
from app.core.security import decode_token
from app.db.models import User
from app.db.session import session_scope

logger = logging.getLogger("helix.realtime")

router = APIRouter(tags=["realtime"])

TRACE_BUFFER_TTL = 300
TRACE_BUFFER_MAX = 200
HEARTBEAT_SECONDS = 20.0


class TraceHub:
    """Fan-out for structured trace events."""

    @staticmethod
    def channel(request_id: str) -> str:
        return f"{TRACE_CHANNEL_PREFIX}{request_id}"

    @staticmethod
    def buffer_key(request_id: str) -> str:
        return f"helix:trace:{request_id}:buffer"

    async def publish(self, event: dict[str, Any]) -> None:
        """Called by `Telemetry.emit_event` from inside the agent graphs."""
        request_id = event.get("request_id")
        if not request_id:
            return
        cache = get_cache()
        try:
            client = await cache.client()
            key = self.buffer_key(request_id)
            payload = json.dumps(event, default=str)
            pipe = client.pipeline()
            pipe.rpush(key, payload)
            pipe.ltrim(key, -TRACE_BUFFER_MAX, -1)
            pipe.expire(key, TRACE_BUFFER_TTL)
            await pipe.execute()
            await client.publish(self.channel(request_id), payload)
        except Exception:
            logger.debug("trace publish failed", exc_info=True)

    async def replay(self, request_id: str) -> list[dict[str, Any]]:
        try:
            client = await get_cache().client()
            raw = await client.lrange(self.buffer_key(request_id), 0, -1)
        except Exception:
            return []
        events = []
        for item in raw:
            try:
                events.append(json.loads(item))
            except json.JSONDecodeError:
                continue
        return events


trace_hub = TraceHub()


async def publish_escalation(event: dict[str, Any]) -> None:
    """Publish a support escalation to every connected admin."""
    try:
        await get_cache().publish(ESCALATIONS_CHANNEL, event)
    except Exception:
        logger.warning("escalation publish failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Handshake auth
# --------------------------------------------------------------------------- #


async def _authenticate(token: str | None, *, require_admin: bool = False) -> User:
    if not token:
        raise AuthError("Missing token")
    claims = decode_token(token, expected_type="access")
    async with session_scope() as session:
        result = await session.execute(select(User).where(User.id == claims["sub"]))
        user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError("Account unavailable")
    if require_admin and user.role != "admin":
        raise AuthError("Admin role required")
    return user


async def _pump(websocket: WebSocket, channel: str, *, event_type: str, pubsub: Any = None) -> None:
    """Forward messages from a Redis channel to a WebSocket until it closes.

    Accepts an already-subscribed `pubsub` so callers can subscribe before doing
    anything else -- see `agent_status` for why that ordering matters.
    """
    pubsub = pubsub or await get_cache().subscribe(channel)
    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                    timeout=HEARTBEAT_SECONDS,
                )
            except TimeoutError:
                # Idle: a heartbeat keeps proxies from dropping the connection
                # and surfaces a dead client promptly.
                await websocket.send_json({"type": "heartbeat"})
                continue
            if message is None:
                await asyncio.sleep(0.01)
                continue
            data = message.get("data")
            if not data:
                continue
            try:
                payload = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue
            await websocket.send_json({"type": event_type, "event": payload})
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()


@router.websocket("/ws/agent-status/{request_id}")
async def agent_status(
    websocket: WebSocket,
    request_id: str,
    token: str | None = Query(default=None, description="JWT access token"),
) -> None:
    """Stream structured agent-step events for one in-flight request."""
    try:
        user = await _authenticate(token)
    except AuthError as exc:
        await websocket.close(code=4401, reason=str(exc))
        return

    await websocket.accept()
    logger.info("trace socket open user=%s request=%s", user.id, request_id)
    pubsub = None
    try:
        await websocket.send_json({"type": "connected", "request_id": request_id})

        # Subscribe BEFORE reading the replay buffer. The other order looks
        # natural but drops events: anything published between the buffer read
        # and the subscription lands in neither, and those steps are gone for
        # good. Subscribing first can only duplicate an event, and the client
        # de-duplicates on `sequence`.
        pubsub = await get_cache().subscribe(TraceHub.channel(request_id))

        buffered = await trace_hub.replay(request_id)
        for event in buffered:
            await websocket.send_json({"type": "trace", "event": event})
        # Explicitly mark the end of the backlog. Without it a client cannot
        # tell "the replay is done" from "the next step is still running", and
        # would have to wait for a heartbeat to find out.
        await websocket.send_json({"type": "replay_complete", "count": len(buffered)})

        await _pump(websocket, TraceHub.channel(request_id), event_type="trace", pubsub=pubsub)
    except WebSocketDisconnect:
        logger.debug("trace socket closed request=%s", request_id)
    except Exception:
        logger.warning("trace socket error request=%s", request_id, exc_info=True)
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)


@router.websocket("/ws/admin/escalations")
async def admin_escalations(
    websocket: WebSocket, token: str | None = Query(default=None, description="JWT access token")
) -> None:
    """Admin-only live feed of escalated support tickets."""
    try:
        user = await _authenticate(token, require_admin=True)
    except AuthError as exc:
        await websocket.close(code=4403, reason=str(exc))
        return

    await websocket.accept()
    logger.info("escalation socket open admin=%s", user.id)
    try:
        await websocket.send_json({"type": "connected", "role": user.role})
        await _pump(websocket, ESCALATIONS_CHANNEL, event_type="escalation")
    except WebSocketDisconnect:
        logger.debug("escalation socket closed admin=%s", user.id)
    except Exception:
        logger.warning("escalation socket error", exc_info=True)
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
