"""The Code Review agent graph.

              +--> quality --+
    START ----+              +--> summarize --> END
              +--> security -+

Quality and security fan out in parallel because they are independent passes
over the same input -- running them concurrently roughly halves wall-clock time,
and LangGraph merges their findings into shared state via the `_extend` reducer.
The summarizer never re-reads the code; it only ranks and merges what the two
reviewers found, which keeps the verdict consistent with the issue list.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.guardrails import validated_completion
from app.core.prompts import (
    CODE_QUALITY_SYSTEM,
    CODE_SECURITY_SYSTEM,
    CODE_SUMMARY_SYSTEM,
    build_prompt,
)
from app.core.telemetry import Telemetry, TracedLLM
from app.schemas.code_review import AgentIssues, CodeIssue, ReviewSummary, Verdict
from app.schemas.common import Severity

logger = logging.getLogger("helix.code_review.agents")

SEVERITY_ORDER = {Severity.critical: 0, Severity.high: 1, Severity.medium: 2, Severity.low: 3}
MAX_CODE_CHARS = 60_000


def _extend(left: list, right: list) -> list:
    return (left or []) + (right or [])


class ReviewState(TypedDict, total=False):
    code: str
    language: str
    filename: str | None
    context: str | None
    # `issues` accumulates via the reducer as each reviewer reports. The
    # summarizer must NOT write back to it -- the reducer would append its
    # merged list to the raw one and double every finding. It writes `merged`,
    # which has no reducer and is therefore replaced rather than extended.
    issues: Annotated[list[dict[str, Any]], _extend]
    merged: list[dict[str, Any]]
    summary: dict[str, Any]
    trace: Annotated[list[dict[str, Any]], _extend]


def number_lines(code: str) -> str:
    """Number the source so the model can reference real line numbers."""
    return "\n".join(f"{i:4d} | {line}" for i, line in enumerate(code.splitlines(), start=1))


class CodeReviewPipeline:
    def __init__(self, llm: TracedLLM, telemetry: Telemetry) -> None:
        self.llm = llm
        self.telemetry = telemetry
        self._sequence = 0
        self.graph = self._build()

    async def _trace(
        self,
        node: str,
        phase: str,
        *,
        message: str = "",
        detail: dict | None = None,
        duration_ms: float | None = None,
    ) -> dict[str, Any]:
        self._sequence += 1
        event = {
            "request_id": self.telemetry.request_id,
            "pod": self.telemetry.pod,
            "node": node,
            "phase": phase,
            "sequence": self._sequence,
            "duration_ms": None if duration_ms is None else round(duration_ms, 2),
            "detail": detail or {},
            "message": message,
        }
        await self.telemetry.emit_event(event)
        return event

    async def _review(
        self, state: ReviewState, *, node: str, system: str, task: str, operation: str, agent: str
    ) -> dict[str, Any]:
        started = time.perf_counter()
        trace = [await self._trace(node, "start", message=f"{agent.capitalize()} review started")]

        result = await validated_completion(
            self.llm,
            AgentIssues,
            build_prompt(
                language=state.get("language", "python"),
                filename=state.get("filename"),
                review_context=state.get("context"),
                code=number_lines(state["code"]),
            ),
            operation=operation,
            task=task,
            system=system,
        )

        issues = []
        for issue in result.issues:
            issue.agent = agent
            issues.append(issue.model_dump(mode="json"))

        counts: dict[str, int] = {}
        for issue in result.issues:
            counts[issue.severity.value] = counts.get(issue.severity.value, 0) + 1

        trace.append(
            await self._trace(
                node,
                "finish",
                message=f"{agent.capitalize()} review found {len(issues)} issue(s)",
                detail={"issue_count": len(issues), "severity_counts": counts},
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return {"issues": issues, "trace": trace}

    async def _node_quality(self, state: ReviewState) -> dict[str, Any]:
        return await self._review(
            state,
            node="quality_agent",
            system=CODE_QUALITY_SYSTEM,
            task="code_quality",
            operation="review.quality",
            agent="quality",
        )

    async def _node_security(self, state: ReviewState) -> dict[str, Any]:
        return await self._review(
            state,
            node="security_agent",
            system=CODE_SECURITY_SYSTEM,
            task="code_security",
            operation="review.security",
            agent="security",
        )

    async def _node_summarize(self, state: ReviewState) -> dict[str, Any]:
        node, started = "summarizer", time.perf_counter()
        raw_issues = state.get("issues", [])
        trace = [
            await self._trace(
                node, "start", message=f"Merging {len(raw_issues)} finding(s) from both reviewers"
            )
        ]

        issues = self._dedupe(raw_issues)
        summary = await validated_completion(
            self.llm,
            ReviewSummary,
            build_prompt(
                language=state.get("language", "python"),
                filename=state.get("filename"),
                findings=_json(issues),
            ),
            operation="review.summarize",
            task="code_summary",
            system=CODE_SUMMARY_SYSTEM,
        )

        # The verdict must follow from the merged list, not from whatever the
        # summarizer felt like saying -- so recompute the counts and enforce the
        # floor for blocking severities.
        counts: dict[str, int] = {}
        for issue in issues:
            counts[issue["severity"]] = counts.get(issue["severity"], 0) + 1
        blocking = counts.get("critical", 0) + counts.get("high", 0)
        if blocking and summary.verdict != Verdict.request_changes:
            summary.verdict = Verdict.request_changes
        elif not issues:
            summary.verdict = Verdict.approve
        summary.severity_counts = counts

        trace.append(
            await self._trace(
                node,
                "finish",
                message=f"Verdict: {summary.verdict.value} ({blocking} blocking of {len(issues)})",
                detail={
                    "verdict": summary.verdict.value,
                    "issue_count": len(issues),
                    "blocking_count": blocking,
                    "severity_counts": counts,
                },
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return {"merged": issues, "summary": summary.model_dump(mode="json"), "trace": trace}

    @staticmethod
    def _dedupe(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop duplicate (line, title) findings and sort by severity then line.

        Both reviewers legitimately flag the same line sometimes -- a hardcoded
        secret is a security issue and a maintainability one -- and the user
        should see it once.
        """
        seen: set[tuple] = set()
        unique: list[dict[str, Any]] = []
        for issue in issues:
            key = (issue.get("line"), issue.get("title", "").strip().lower())
            if key in seen:
                continue
            seen.add(key)
            unique.append(issue)
        return sorted(
            unique,
            key=lambda i: (
                SEVERITY_ORDER.get(Severity(i.get("severity", "low")), 9),
                i.get("line") or 10**6,
            ),
        )

    def _build(self):
        graph = StateGraph(ReviewState)
        graph.add_node("quality", self._node_quality)
        graph.add_node("security", self._node_security)
        graph.add_node("summarize", self._node_summarize)

        # Two edges out of START = concurrent execution; summarize waits for both.
        graph.add_edge(START, "quality")
        graph.add_edge(START, "security")
        graph.add_edge("quality", "summarize")
        graph.add_edge("security", "summarize")
        graph.add_edge("summarize", END)
        return graph.compile()

    async def run(
        self, code: str, *, language: str = "python", filename: str | None = None, context: str | None = None
    ) -> dict[str, Any]:
        if len(code) > MAX_CODE_CHARS:
            raise ValueError(f"Snippet exceeds the {MAX_CODE_CHARS} character limit")

        final: ReviewState = await self.graph.ainvoke(
            {
                "code": code,
                "language": language,
                "filename": filename,
                "context": context,
                "issues": [],
                "merged": [],
                "trace": [],
            }
        )

        issues = [CodeIssue.model_validate(i) for i in final.get("merged", [])]
        summary = ReviewSummary.model_validate(
            final.get("summary") or {"verdict": "comment", "summary": "Review completed."}
        )
        return {
            "issues": issues,
            "verdict": summary.verdict,
            "summary": summary.summary,
            "severity_counts": summary.severity_counts,
            "top_recommendation": summary.top_recommendation,
            "issue_count": len(issues),
            "blocking_count": sum(1 for i in issues if i.is_blocking),
            "trace": final.get("trace", []),
        }


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str)
