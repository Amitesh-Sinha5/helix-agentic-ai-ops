"""The Support Triage agent graph.

    classify -> kb_retrieve -> draft -> escalate

`classify` is the interesting node. It runs the trained scikit-learn classifier
first; if the model clears `CLASSIFIER_CONFIDENCE_THRESHOLD` the ticket is
classified with **no LLM call at all** -- sub-millisecond and free. Only the
genuinely ambiguous minority falls through to an LLM classifier agent. Which
path ran is recorded in telemetry and on the ticket row, so the saving is
measurable rather than asserted.

KB retrieval reuses the Phase 4 hybrid retriever against a `knowledge_base`
collection, so the drafted reply is grounded in real support content instead of
being invented.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.config import Settings, get_settings
from app.core.guardrails import redact_pii, validated_completion
from app.core.prompts import (
    ESCALATION_SYSTEM,
    SUPPORT_DRAFT_SYSTEM,
    TICKET_CLASSIFICATION_SYSTEM,
    build_prompt,
    format_context,
)
from app.core.telemetry import Telemetry, TracedLLM
from app.rag.agents import owner_scope
from app.rag.retrieval import hybrid_search
from app.rag.vectorstore import VectorStore, get_vector_store
from app.schemas.support import Classification, ClassificationResult, EscalationDecision, KBSource
from app.support.classifier import get_classifier

logger = logging.getLogger("helix.support.agents")

PATH_TRAINED = "trained_model"
PATH_LLM = "llm_fallback"


def _extend(left: list, right: list) -> list:
    return (left or []) + (right or [])


class TriageState(TypedDict, total=False):
    subject: str
    body: str
    text: str
    collection: str
    owner_id: str | None

    classification: dict[str, Any]
    kb_sources: list[dict[str, Any]]
    kb_context: str
    draft_response: str
    escalation: dict[str, Any]
    trace: Annotated[list[dict[str, Any]], _extend]


class SupportTriagePipeline:
    def __init__(
        self,
        llm: TracedLLM,
        telemetry: Telemetry,
        *,
        store: VectorStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.llm = llm
        self.telemetry = telemetry
        self.store = store or get_vector_store()
        self.settings = settings or get_settings()
        self.classifier = get_classifier()
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

    # -- nodes ------------------------------------------------------------
    async def _node_classify(self, state: TriageState) -> dict[str, Any]:
        node, started = "classifier", time.perf_counter()
        trace = [await self._trace(node, "start", message="Classifying priority and category")]

        text = state["text"]
        async with self.telemetry.span("support.classify_trained") as measurement:
            prediction = self.classifier.predict(text)
            measurement.extra = {
                "available": prediction.available,
                "confidence": prediction.confidence,
                "priority": prediction.priority,
                "category": prediction.category,
            }

        if prediction.is_confident:
            result = ClassificationResult(
                priority=prediction.priority,
                category=prediction.category,
                confidence=prediction.confidence,
                reason=(
                    f"Trained TF-IDF + logistic-regression classifier, confidence "
                    f"{prediction.confidence:.2f} (>= {self.settings.classifier_confidence_threshold})."
                ),
                path=PATH_TRAINED,
                model_confidence=prediction.confidence,
                probabilities=prediction.probabilities,
            )
            message = (
                f"Trained model: {result.priority.value}/{result.category.value} "
                f"at {prediction.confidence:.2f} confidence - no LLM call needed"
            )
        else:
            # Low confidence or no artefact: spend an LLM call rather than guess.
            llm_result = await validated_completion(
                self.llm,
                Classification,
                build_prompt(ticket=text),
                operation="support.classify_llm",
                task="ticket_classification",
                system=TICKET_CLASSIFICATION_SYSTEM,
            )
            result = ClassificationResult(
                priority=llm_result.priority,
                category=llm_result.category,
                confidence=llm_result.confidence,
                reason=(
                    f"Trained classifier confidence {prediction.confidence:.2f} was below the "
                    f"{self.settings.classifier_confidence_threshold} threshold"
                    if prediction.available
                    else "Trained classifier unavailable"
                )
                + f"; escalated to the LLM classifier. {llm_result.reason}",
                path=PATH_LLM,
                model_confidence=prediction.confidence if prediction.available else None,
                probabilities=prediction.probabilities,
            )
            message = (
                f"LLM fallback: {result.priority.value}/{result.category.value} "
                f"(trained model only reached {prediction.confidence:.2f})"
            )

        self.telemetry.annotate_last(classification_path=result.path)
        trace.append(
            await self._trace(
                node,
                "finish",
                message=message,
                detail={
                    "path": result.path,
                    "priority": result.priority.value,
                    "category": result.category.value,
                    "confidence": round(result.confidence, 3),
                    "model_confidence": result.model_confidence,
                    "threshold": self.settings.classifier_confidence_threshold,
                },
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return {"classification": result.model_dump(mode="json"), "trace": trace}

    async def _node_kb_retrieve(self, state: TriageState) -> dict[str, Any]:
        node, started = "kb_retriever", time.perf_counter()
        trace = [await self._trace(node, "start", message="Searching the knowledge base")]

        query = f"{state['subject']} {state['body']}"
        embedding = await self.llm.embed_one(query, operation="support.embed_query")
        async with self.telemetry.span("support.kb_search") as measurement:
            result = await hybrid_search(
                query,
                embedding=embedding,
                collection=state["collection"],
                store=self.store,
                where=owner_scope(state.get("owner_id")),
            )
            measurement.extra = {"kept": len(result.candidates), "reranker": result.reranker}

        relevant = [c for c in result.candidates if c.final_score >= self.settings.min_retrieval_score]

        # Two different things, deliberately kept apart: `sources` carries a
        # short snippet for the UI, while `kb_context` keeps the *full* chunk
        # text for the draft agent. Grounding an agent on a display-truncated
        # snippet silently drops the end of the passage -- which is usually the
        # part containing the actual resolution.
        sources = [
            KBSource(
                document_id=c.chunk.document_id,
                title=c.chunk.document_title,
                snippet=c.chunk.text[:400],
                score=round(c.final_score, 4),
            ).model_dump(mode="json")
            for c in relevant
        ]
        kb_context = format_context(
            [
                {"text": c.chunk.text, "source": c.chunk.document_title or c.chunk.document_id}
                for c in relevant
            ]
        )

        trace.append(
            await self._trace(
                node,
                "finish",
                message=f"Found {len(sources)} relevant knowledge-base passage(s)",
                detail={
                    "sources": len(sources),
                    "vector_hits": result.vector_hits,
                    "keyword_hits": result.keyword_hits,
                    "reranker": result.reranker,
                    "top_score": round(result.top_score, 4),
                },
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return {"kb_sources": sources, "kb_context": kb_context, "trace": trace}

    async def _node_draft(self, state: TriageState) -> dict[str, Any]:
        node, started = "draft_agent", time.perf_counter()
        trace = [await self._trace(node, "start", message="Drafting a grounded reply")]

        sources = state.get("kb_sources", [])
        kb_context = state.get("kb_context", "")
        classification = state.get("classification", {})

        response = await self.llm.complete(
            build_prompt(
                ticket=state["text"],
                priority=classification.get("priority", ""),
                category=classification.get("category", ""),
                knowledge_base=kb_context or "(no relevant knowledge-base content found)",
            ),
            operation="support.draft",
            task="support_draft",
            system=SUPPORT_DRAFT_SYSTEM,
        )
        # The draft is customer-facing and may echo details from the ticket, so
        # it is scrubbed before it can be stored or displayed.
        draft = redact_pii(response.text.strip())

        trace.append(
            await self._trace(
                node,
                "finish",
                message="Draft reply generated",
                detail={"chars": len(draft), "grounded_in": len(sources)},
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return {"draft_response": draft, "trace": trace}

    async def _node_escalate(self, state: TriageState) -> dict[str, Any]:
        node, started = "escalation_agent", time.perf_counter()
        trace = [await self._trace(node, "start", message="Deciding whether a human is needed")]

        classification = state.get("classification", {})
        decision = await validated_completion(
            self.llm,
            EscalationDecision,
            build_prompt(
                ticket=state["text"],
                priority=classification.get("priority", ""),
                category=classification.get("category", ""),
                knowledge_base_hits=str(len(state.get("kb_sources", []))),
            ),
            operation="support.escalation",
            task="escalation",
            system=ESCALATION_SYSTEM,
        )

        # Urgent always reaches a human, whatever the model concluded.
        if classification.get("priority") == "urgent" and not decision.escalate:
            decision.escalate = True
            decision.reason = f"Priority is urgent, so escalation is enforced. {decision.reason}".strip()
            decision.suggested_owner = decision.suggested_owner or "on-call-engineer"

        trace.append(
            await self._trace(
                node,
                "finish",
                message=("Escalating to a human" if decision.escalate else "No escalation needed"),
                detail={
                    "escalate": decision.escalate,
                    "reason": decision.reason,
                    "suggested_owner": decision.suggested_owner,
                },
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return {"escalation": decision.model_dump(mode="json"), "trace": trace}

    # -- graph ------------------------------------------------------------
    def _build(self):
        graph = StateGraph(TriageState)
        graph.add_node("classify", self._node_classify)
        graph.add_node("kb_retrieve", self._node_kb_retrieve)
        graph.add_node("draft", self._node_draft)
        graph.add_node("escalate", self._node_escalate)

        graph.add_edge(START, "classify")
        graph.add_edge("classify", "kb_retrieve")
        graph.add_edge("kb_retrieve", "draft")
        graph.add_edge("draft", "escalate")
        graph.add_edge("escalate", END)
        return graph.compile()

    async def run(
        self,
        subject: str,
        body: str,
        *,
        collection: str = "knowledge_base",
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        final: TriageState = await self.graph.ainvoke(
            {
                "subject": subject,
                "body": body,
                "text": f"{subject}\n\n{body}".strip(),
                "collection": collection,
                "owner_id": owner_id,
                "trace": [],
            }
        )
        classification = final.get("classification", {})
        escalation = final.get("escalation", {})
        return {
            "priority": classification.get("priority", "medium"),
            "category": classification.get("category", "general"),
            "confidence": classification.get("confidence", 0.0),
            "classification_path": classification.get("path", PATH_LLM),
            "classification_reason": classification.get("reason", ""),
            "draft_response": final.get("draft_response", ""),
            "kb_sources": final.get("kb_sources", []),
            "escalate": bool(escalation.get("escalate")),
            "escalation_reason": escalation.get("reason"),
            "suggested_owner": escalation.get("suggested_owner"),
            "trace": final.get("trace", []),
        }
