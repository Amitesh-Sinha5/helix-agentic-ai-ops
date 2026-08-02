"""Tools the Doc Q&A agent can call before answering.

Some questions cannot be answered from documents no matter how good retrieval
is -- "where is my order ORD1042?" needs live system state. The tool-router node
detects those, calls the tool, and folds the result into the answer context.

`lookup_order_status` is a deliberately mock backend: deterministic, offline, and
shaped like the real integration would be.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger("helix.tools")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, str]
    handler: Callable[..., Awaitable[dict[str, Any]]]

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        return await self.handler(**kwargs)


_STATUSES = ("processing", "packed", "in_transit", "out_for_delivery", "delivered")
_CARRIERS = ("Fleetline", "Northwind Freight", "Pacific Post")


async def lookup_order_status(order_id: str | None = None, **_: Any) -> dict[str, Any]:
    """Mock order-status backend.

    Deterministic from the order id, so the same question always yields the same
    answer and the pipeline stays testable.
    """
    if not order_id:
        return {"found": False, "error": "order_id is required"}
    normalised = str(order_id).strip().upper()
    seed = int(hashlib.blake2b(normalised.encode(), digest_size=8).hexdigest(), 16)
    if seed % 17 == 0:  # a slice of ids are genuinely unknown
        return {"found": False, "order_id": normalised, "error": "No order found with that reference"}

    status = _STATUSES[seed % len(_STATUSES)]
    days_out = seed % 5
    eta = datetime.now(UTC) + timedelta(days=days_out)
    return {
        "found": True,
        "order_id": normalised,
        "status": status,
        "carrier": _CARRIERS[(seed >> 8) % len(_CARRIERS)],
        "tracking_number": f"HX{seed % 10**9:09d}",
        "estimated_delivery": eta.date().isoformat(),
        "last_update": datetime.now(UTC).isoformat(),
    }


REGISTRY: dict[str, Tool] = {
    "lookup_order_status": Tool(
        name="lookup_order_status",
        description="Look up the live status, carrier and ETA of a customer order by its reference.",
        parameters={"order_id": "The order reference, e.g. ORD1042"},
        handler=lookup_order_status,
    )
}


def tool_catalogue() -> str:
    """Render the registry for inclusion in a prompt."""
    return "\n".join(f"- {t.name}({', '.join(t.parameters)}): {t.description}" for t in REGISTRY.values())


async def invoke_tool(name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], float]:
    """Call a registered tool. Returns (result, latency_ms)."""
    tool = REGISTRY.get(name)
    started = time.perf_counter()
    if tool is None:
        return {"found": False, "error": f"Unknown tool {name!r}"}, 0.0
    try:
        result = await tool(**arguments)
    except Exception as exc:
        logger.warning("tool %s failed", name, exc_info=True)
        result = {"found": False, "error": f"{type(exc).__name__}: {exc}"}
    return result, (time.perf_counter() - started) * 1000


def format_tool_result(name: str, result: dict[str, Any]) -> str:
    """Render a tool result as a context block the answer agent can cite."""
    if not result.get("found"):
        return f"[tool:{name}] No result: {result.get('error', 'not found')}."
    fields = ", ".join(f"{k}={v}" for k, v in result.items() if k not in ("found",))
    return f"[tool:{name}] {fields}."
