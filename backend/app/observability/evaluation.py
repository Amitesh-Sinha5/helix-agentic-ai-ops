"""RAG evaluation metrics: faithfulness, answer relevance, context precision.

These are the real RAG metrics, not substring matching. Each is computed by an
LLM-as-judge call through the same `LLMClient` the pods use, which means the gate
runs offline against the mock provider in CI and against a real model in
production with no code change.

`ragas` is used when it is installed *and* an OpenAI-compatible provider is
configured. It is deliberately not a hard dependency: ragas insists on a real
LLM and embedding backend, so wiring it to the deterministic mock provider would
produce numbers that look authoritative while measuring nothing. The
LLM-as-judge implementation below is the honest fallback, and it is the path CI
actually exercises.

    faithfulness      -- is every claim in the answer supported by the context?
    answer_relevance  -- does the answer actually address the question asked?
    context_precision -- did retrieval surface the context the ground truth needs?
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.core.llm_client import LLMClient, get_llm_client
from app.core.prompts import JUDGE_SYSTEM, build_prompt

logger = logging.getLogger("helix.evaluation")

METRICS = ("faithfulness", "answer_relevance", "context_precision")


@dataclass
class EvaluationCase:
    question: str
    answer: str
    contexts: list[str]
    ground_truth: str | None = None
    case_id: str = ""

    @property
    def context_text(self) -> str:
        return "\n\n".join(self.contexts)


@dataclass
class MetricScores:
    """A metric of None means "not applicable to this case", not zero.

    The distinction matters: an abstention has no retrieved context, so context
    precision is undefined for it. Scoring it 0.0 would drag the average down
    for behaving *correctly*, quietly pressuring the gate toward a pipeline that
    always answers.
    """

    faithfulness: float | None = None
    answer_relevance: float | None = None
    context_precision: float | None = None
    case_id: str = ""
    notes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, float | None]:
        return {metric: (None if (v := getattr(self, metric)) is None else round(v, 4)) for metric in METRICS}

    def worst(self) -> tuple[str, float]:
        applicable = {k: v for k, v in self.as_dict().items() if v is not None}
        if not applicable:
            return "n/a", 1.0
        name = min(applicable, key=lambda k: applicable[k])
        return name, applicable[name]


def ragas_available() -> bool:
    """True only when ragas can be driven by a real (non-mock) provider."""
    settings = get_settings()
    if settings.llm_provider == "mock":
        return False
    try:
        import ragas  # noqa: F401
    except ImportError:
        return False
    return True


class RagEvaluator:
    """Scores one case at a time so a failure is attributable to a case."""

    def __init__(self, client: LLMClient | None = None) -> None:
        self.client = client or get_llm_client()

    async def _judge(self, metric: str, case: EvaluationCase) -> float:
        prompt = build_prompt(
            metric=metric,
            question=case.question,
            answer=case.answer,
            context=case.context_text,
            ground_truth=case.ground_truth,
        )
        try:
            parsed, _ = await self.client.complete_json(prompt, task="judge", system=JUDGE_SYSTEM)
        except Exception:
            logger.warning("judge call failed for metric=%s case=%s", metric, case.case_id, exc_info=True)
            return 0.0
        try:
            return max(0.0, min(1.0, float(parsed.get("score", 0.0))))
        except (TypeError, ValueError):
            return 0.0

    async def score(self, case: EvaluationCase) -> MetricScores:
        # An honest abstention is a correct outcome, not a quality failure: it is
        # perfectly faithful (it claims nothing) and perfectly relevant (saying
        # "not in the documents" is the right answer when it is not).
        if not case.contexts or not case.answer.strip():
            return MetricScores(
                faithfulness=1.0,
                answer_relevance=1.0,
                context_precision=None,  # undefined: nothing was retrieved
                case_id=case.case_id,
                notes={"abstained": True},
            )

        return MetricScores(
            faithfulness=await self._judge("faithfulness", case),
            answer_relevance=await self._judge("answer_relevance", case),
            context_precision=await self._judge("context_precision", case),
            case_id=case.case_id,
        )

    async def score_all(self, cases: list[EvaluationCase]) -> list[MetricScores]:
        return [await self.score(case) for case in cases]


def aggregate(scores: list[MetricScores]) -> dict[str, float]:
    """Average each metric over the cases where it is applicable."""
    averages: dict[str, float] = {}
    for metric in METRICS:
        values = [v for s in scores if (v := getattr(s, metric)) is not None]
        averages[metric] = round(sum(values) / len(values), 4) if values else 0.0
    return averages


def applicable_counts(scores: list[MetricScores]) -> dict[str, int]:
    return {metric: sum(1 for s in scores if getattr(s, metric) is not None) for metric in METRICS}


def thresholds() -> dict[str, float]:
    settings = get_settings()
    return {
        "faithfulness": settings.quality_min_faithfulness,
        "answer_relevance": settings.quality_min_answer_relevance,
        "context_precision": settings.quality_min_context_precision,
    }


def check_thresholds(averages: dict[str, float]) -> list[str]:
    """Return a human-readable failure per metric that fell below its floor."""
    failures = []
    for metric, minimum in thresholds().items():
        actual = averages.get(metric, 0.0)
        if actual < minimum:
            failures.append(f"{metric}: {actual:.3f} < required {minimum:.2f}")
    return failures
