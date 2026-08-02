"""Session-scoped conversation memory for multi-turn Doc Q&A.

Turns live in Redis under a TTL rather than in Postgres: conversation context is
inherently ephemeral, and this keeps follow-up latency at one round trip.

Its real job is question resolution. "Does that apply to annual plans?" is
unanswerable on its own -- `resolve_followup` rewrites it against the previous
turn so retrieval sees a self-contained query.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.cache import get_cache
from app.core.llm_client import content_words

logger = logging.getLogger("helix.memory")

MEMORY_TTL_SECONDS = 3600
MAX_TURNS = 8

_FOLLOWUP_MARKERS = (
    "that",
    "it",
    "this",
    "those",
    "them",
    "they",
    "he",
    "she",
    "instead",
    "also",
    "too",
    "same",
    "one",
)


@dataclass
class Turn:
    question: str
    answer: str
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _key(session_id: str) -> str:
    return f"helix:memory:{session_id}"


class ConversationMemory:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    async def turns(self) -> list[Turn]:
        try:
            raw = await get_cache().get_json(_key(self.session_id)) or []
        except Exception:
            logger.debug("memory read failed", exc_info=True)
            return []
        return [Turn(**t) for t in raw if isinstance(t, dict)]

    async def append(self, question: str, answer: str) -> None:
        turns = await self.turns()
        turns.append(Turn(question=question, answer=answer))
        try:
            await get_cache().set_json(
                _key(self.session_id),
                [t.as_dict() for t in turns[-MAX_TURNS:]],
                ttl=MEMORY_TTL_SECONDS,
            )
        except Exception:
            logger.debug("memory write failed", exc_info=True)

    async def clear(self) -> None:
        await get_cache().delete(_key(self.session_id))

    async def as_context(self, limit: int = 3) -> str:
        turns = await self.turns()
        if not turns:
            return ""
        return "\n".join(f"Q: {t.question}\nA: {t.answer}" for t in turns[-limit:])


def is_followup(question: str) -> bool:
    """Heuristic: short, and either pronoun-led or missing its own subject."""
    words = question.lower().split()
    if len(words) > 14:
        return False
    return any(marker in words for marker in _FOLLOWUP_MARKERS) or len(content_words(question)) <= 3


def resolve_followup(question: str, turns: list[Turn]) -> str:
    """Expand a follow-up into a self-contained retrieval query.

    Carries forward the previous question's content words that the follow-up
    does not already mention, so retrieval is not left resolving "that".
    """
    if not turns or not is_followup(question):
        return question
    previous = turns[-1].question
    existing = set(content_words(question))
    carried = [w for w in content_words(previous) if w not in existing]
    if not carried:
        return question
    return f"{question} ({' '.join(carried[:8])})"
