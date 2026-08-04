"""The Doc Q&A agent graph.

    tool_router -> retrieve -> assess ---(sufficient)---> answer -> critique -> validate
                      ^                 |
                      |            (insufficient, iterations < budget)
                      +--- reformulate -+

This is agentic retrieval rather than a single retrieve-then-generate pass. The
`assess` node judges whether what came back can actually answer the question; if
it cannot, `reformulate` rewrites the query using vocabulary observed in the
first pass (pseudo-relevance feedback) and retrieval runs again, up to
`MAX_RETRIEVAL_LOOPS` times. The graph abstains rather than guessing: if nothing
clears `MIN_RETRIEVAL_SCORE`, or the final groundedness check fails, the answer
becomes an explicit "not found".

Every node emits a structured trace event, which is what the frontend renders as
a live reasoning trace instead of a spinner.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.config import Settings, get_settings
from app.core.guardrails import GuardrailViolation, redact_pii, validated_completion
from app.core.prompts import (
    CONTEXT_SUFFICIENCY_SYSTEM,
    GROUNDEDNESS_SYSTEM,
    QUERY_REFORMULATION_SYSTEM,
    RAG_ANSWER_SYSTEM,
    SELF_CRITIQUE_SYSTEM,
    TOOL_ROUTER_SYSTEM,
    build_prompt,
    format_context,
)
from app.core.telemetry import Telemetry, TracedLLM
from app.rag import tools as tool_module
from app.rag.memory import ConversationMemory, Turn, resolve_followup
from app.rag.retrieval import hybrid_search
from app.rag.vectorstore import VectorStore, get_vector_store
from app.schemas.rag import (
    Citation,
    CritiqueResult,
    GroundednessVerdict,
    ReformulatedQuery,
    SufficiencyVerdict,
    ToolDecision,
)

logger = logging.getLogger("helix.rag.agents")

NOT_FOUND = "I could not find that in the provided documents."


async def _degradable(coro, fallback: Any, node: str) -> Any:
    """Run a guardrailed node call, falling back instead of failing the request.

    Used only for nodes whose result is an *optimisation*, never for the ones
    that keep the answer honest. A weaker model (or a bad day for a strong one)
    can fail to produce schema-valid JSON; when that happens on an optional node
    the right behaviour is to carry on without it, not to 500 the whole query.

    Groundedness deliberately does NOT use this: it fails closed, because
    "we could not verify this answer" must never degrade into "ship it".
    """
    try:
        return await coro
    except GuardrailViolation as exc:
        logger.warning("node %s produced unusable output, degrading: %s", node, exc)
        return fallback


def owner_scope(owner_id: str | None) -> dict[str, str] | None:
    """Retrieval filter restricting results to one owner's chunks.

    Returns None when no owner is given, which means "search everything" -- used
    by evaluation and admin tooling, never by a user-facing request.
    """
    return {"owner_id": owner_id} if owner_id else None


def _extend(left: list, right: list) -> list:
    return (left or []) + (right or [])


class GraphState(TypedDict, total=False):
    question: str
    current_query: str
    collection: str
    top_k: int | None
    self_critique: bool
    owner_id: str | None

    chunks: list[dict[str, Any]]
    retrieval_meta: dict[str, Any]
    iterations: int
    reformulated_queries: Annotated[list[str], _extend]
    sufficiency: dict[str, Any]

    conversation_context: str
    tool_context: str
    tool_invocations: Annotated[list[dict[str, Any]], _extend]

    answer: str
    critique: str | None
    groundedness: dict[str, Any]
    found: bool
    trace: Annotated[list[dict[str, Any]], _extend]


class DocQAPipeline:
    """Builds and runs the Doc Q&A graph for a single request."""

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
        self._sequence = 0
        self.graph = self._build()

    # -- tracing ----------------------------------------------------------
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
    async def _node_tool_router(self, state: GraphState) -> dict[str, Any]:
        node, started = "tool_router", time.perf_counter()
        trace = [await self._trace(node, "start", message="Checking whether an external tool is needed")]

        decision = await _degradable(
            validated_completion(
                self.llm,
                ToolDecision,
                build_prompt(
                    question=state["question"],
                    available_tools=tool_module.tool_catalogue(),
                ),
                operation="rag.tool_router",
                task="tool_router",
                system=TOOL_ROUTER_SYSTEM,
            ),
            fallback=ToolDecision(tool=None),
            node="tool_router",
        )

        updates: dict[str, Any] = {}
        if decision.tool:
            result, latency = await tool_module.invoke_tool(decision.tool, decision.arguments)
            updates["tool_context"] = tool_module.format_tool_result(decision.tool, result)
            updates["tool_invocations"] = [
                {
                    "tool": decision.tool,
                    "arguments": decision.arguments,
                    "result": result,
                    "latency_ms": round(latency, 2),
                }
            ]
            message = f"Called {decision.tool}({decision.arguments})"
            detail = {"tool": decision.tool, "arguments": decision.arguments, "found": result.get("found")}
        else:
            message = "No tool required; answering from documents"
            detail = {"tool": None}

        trace.append(
            await self._trace(
                node,
                "finish",
                message=message,
                detail=detail,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return {**updates, "trace": trace}

    async def _node_retrieve(self, state: GraphState) -> dict[str, Any]:
        node, started = "retriever", time.perf_counter()
        query = state.get("current_query") or state["question"]
        iteration = state.get("iterations", 0) + 1
        trace = [
            await self._trace(
                node, "start", message=f"Hybrid search (pass {iteration}): {query!r}", detail={"query": query}
            )
        ]

        embedding = await self.llm.embed_one(query, operation="rag.embed_query")
        async with self.telemetry.span("rag.hybrid_search") as measurement:
            result = await hybrid_search(
                query,
                embedding=embedding,
                collection=state["collection"],
                top_k=state.get("top_k") or self.settings.retrieval_top_k,
                store=self.store,
                where=owner_scope(state.get("owner_id")),
            )
            measurement.extra = {
                "vector_hits": result.vector_hits,
                "keyword_hits": result.keyword_hits,
                "fused_hits": result.fused_hits,
                "kept": len(result.candidates),
                "reranker": result.reranker,
            }

        meta = {
            "query": query,
            "vector_hits": result.vector_hits,
            "keyword_hits": result.keyword_hits,
            "fused_hits": result.fused_hits,
            "kept": len(result.candidates),
            "reranker": result.reranker,
            "top_score": round(result.top_score, 4),
            "rerank_scores": [round(c.rerank_score or 0.0, 4) for c in result.candidates],
            **result.timings_ms,
        }
        trace.append(
            await self._trace(
                node,
                "finish",
                message=(
                    f"Vector {result.vector_hits} + keyword {result.keyword_hits} "
                    f"-> fused {result.fused_hits} -> {result.reranker} kept "
                    f"{len(result.candidates)} (top score {result.top_score:.3f})"
                ),
                detail=meta,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return {
            "chunks": result.chunks,
            "retrieval_meta": meta,
            "iterations": iteration,
            "current_query": query,
            "trace": trace,
        }

    async def _node_assess(self, state: GraphState) -> dict[str, Any]:
        node, started = "context_check", time.perf_counter()
        trace = [
            await self._trace(node, "start", message="Judging whether the context can answer the question")
        ]

        chunks = state.get("chunks", [])
        top_score = max((c["score"] for c in chunks), default=0.0)

        # Nothing cleared the retrieval-score floor: that is a decision we can
        # make without spending an LLM call.
        if not chunks or top_score < self.settings.min_retrieval_score:
            verdict = SufficiencyVerdict(
                sufficient=False,
                confidence=1.0 - top_score,
                reason=(
                    "No chunks retrieved."
                    if not chunks
                    else f"Best chunk scored {top_score:.3f}, below the {self.settings.min_retrieval_score} floor."
                ),
                missing_information=[state["question"]],
            )
        else:
            verdict = await _degradable(
                validated_completion(
                    self.llm,
                    SufficiencyVerdict,
                    build_prompt(question=state["question"], context=format_context(chunks)),
                    operation="rag.context_sufficiency",
                    task="context_sufficiency",
                    system=CONTEXT_SUFFICIENCY_SYSTEM,
                ),
                # Assume sufficient: the retrieved context already cleared the
                # score floor, and the validator still has the final say.
                fallback=SufficiencyVerdict(
                    sufficient=True, confidence=0.0, reason="Sufficiency check unavailable."
                ),
                node="context_check",
            )

        trace.append(
            await self._trace(
                node,
                "finish",
                message=("Context sufficient" if verdict.sufficient else f"Insufficient: {verdict.reason}"),
                detail={
                    "sufficient": verdict.sufficient,
                    "confidence": round(verdict.confidence, 3),
                    "top_score": round(top_score, 4),
                    "missing_information": verdict.missing_information,
                    "iteration": state.get("iterations", 1),
                },
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return {"sufficiency": verdict.model_dump(), "trace": trace}

    def _route_after_assess(self, state: GraphState) -> str:
        if state.get("sufficiency", {}).get("sufficient"):
            return "answer"
        if state.get("iterations", 1) < self.settings.max_retrieval_loops:
            return "reformulate"
        return "answer"

    async def _node_reformulate(self, state: GraphState) -> dict[str, Any]:
        node, started = "reformulate", time.perf_counter()
        trace = [await self._trace(node, "start", message="Rewriting the query and retrying retrieval")]

        sufficiency = state.get("sufficiency", {})
        reformulated = await _degradable(
            validated_completion(
                self.llm,
                ReformulatedQuery,
                build_prompt(
                    question=state["question"],
                    failed_query=state.get("current_query", ""),
                    missing_information=", ".join(sufficiency.get("missing_information", [])),
                    # Pseudo-relevance feedback: the first pass's vocabulary is the
                    # best available hint about how the corpus words this topic.
                    context=format_context(state.get("chunks", [])[:3]),
                ),
                operation="rag.query_reformulation",
                task="query_reformulation",
                system=QUERY_REFORMULATION_SYSTEM,
            ),
            fallback=ReformulatedQuery(
                query=state.get("current_query") or state["question"],
                rationale="Reformulation unavailable; retrying the original query.",
            ),
            node="reformulate",
        )

        trace.append(
            await self._trace(
                node,
                "finish",
                message=f"Reformulated to: {reformulated.query!r}",
                detail={"query": reformulated.query, "rationale": reformulated.rationale},
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return {
            "current_query": reformulated.query,
            "reformulated_queries": [reformulated.query],
            "trace": trace,
        }

    async def _node_answer(self, state: GraphState) -> dict[str, Any]:
        node, started = "answer", time.perf_counter()
        trace = [await self._trace(node, "start", message="Generating the answer from validated context")]

        chunks = state.get("chunks", [])
        usable = [c for c in chunks if c["score"] >= self.settings.min_retrieval_score]
        tool_context = state.get("tool_context", "")

        if not usable and not tool_context:
            answer, found = NOT_FOUND, False
            message = "Abstained: no retrieved context cleared the relevance floor"
        else:
            context = format_context(usable)
            if tool_context:
                context = f"{context}\n\n{tool_context}" if context else tool_context
            response = await self.llm.complete(
                build_prompt(
                    question=state["question"],
                    context=context,
                    conversation=state.get("conversation_context", ""),
                ),
                operation="rag.answer",
                task="rag_answer",
                system=RAG_ANSWER_SYSTEM,
            )
            answer = redact_pii(response.text.strip())
            found = NOT_FOUND.lower() not in answer.lower()
            message = "Answer generated" if found else "Model abstained: answer not present in context"

        trace.append(
            await self._trace(
                node,
                "finish",
                message=message,
                detail={
                    "found": found,
                    "chunks_used": len(usable),
                    "tool_context_used": bool(tool_context),
                    "answer_chars": len(answer),
                },
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return {"answer": answer, "found": found, "trace": trace}

    async def _node_critique(self, state: GraphState) -> dict[str, Any]:
        node, started = "self_critique", time.perf_counter()
        if not state.get("self_critique", True) or not state.get("found"):
            return {"critique": None}
        trace = [await self._trace(node, "start", message="Reviewing the draft answer before returning it")]

        chunks = [c for c in state.get("chunks", []) if c["score"] >= self.settings.min_retrieval_score]
        result = await _degradable(
            validated_completion(
                self.llm,
                CritiqueResult,
                build_prompt(
                    question=state["question"],
                    draft_answer=state.get("answer", ""),
                    context=format_context(chunks),
                ),
                operation="rag.self_critique",
                task="self_critique",
                system=SELF_CRITIQUE_SYSTEM,
            ),
            fallback=CritiqueResult(revised_answer=state.get("answer", ""), changed=False, critique=""),
            node="self_critique",
        )

        answer = redact_pii(result.revised_answer.strip()) or state.get("answer", "")
        trace.append(
            await self._trace(
                node,
                "finish",
                message=("Answer revised" if result.changed else "No revision needed"),
                detail={"changed": result.changed, "critique": result.critique},
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return {"answer": answer, "critique": result.critique, "trace": trace}

    async def _node_validate(self, state: GraphState) -> dict[str, Any]:
        node, started = "validator", time.perf_counter()
        trace = [await self._trace(node, "start", message="Checking the answer is grounded in the context")]

        chunks = [c for c in state.get("chunks", []) if c["score"] >= self.settings.min_retrieval_score]
        answer = state.get("answer", "")

        if not state.get("found"):
            verdict = GroundednessVerdict(grounded=True, score=1.0, reason="Abstained; nothing to ground.")
        else:
            verdict = await _degradable(
                validated_completion(
                    self.llm,
                    GroundednessVerdict,
                    build_prompt(question=state["question"], answer=answer, context=format_context(chunks)),
                    operation="rag.groundedness",
                    task="groundedness",
                    system=GROUNDEDNESS_SYSTEM,
                ),
                # Fail CLOSED. If we cannot verify the answer we must not claim
                # it is grounded -- the node below turns this into an abstention.
                fallback=GroundednessVerdict(
                    grounded=False, score=0.0, reason="Groundedness check unavailable."
                ),
                node="validator",
            )

        updates: dict[str, Any] = {"groundedness": verdict.model_dump()}
        # A confident-sounding but unsupported answer is worse than no answer.
        if state.get("found") and not verdict.grounded:
            updates["answer"] = NOT_FOUND
            updates["found"] = False
            message = f"Rejected as ungrounded ({verdict.score:.2f}); returning 'not found'"
        else:
            message = f"Grounded ({verdict.score:.2f})"

        trace.append(
            await self._trace(
                node,
                "finish",
                message=message,
                detail={
                    "grounded": verdict.grounded,
                    "score": round(verdict.score, 3),
                    "reason": verdict.reason,
                },
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        updates["trace"] = trace
        return updates

    # -- graph ------------------------------------------------------------
    def _build(self):
        graph = StateGraph(GraphState)
        graph.add_node("tool_router", self._node_tool_router)
        graph.add_node("retrieve", self._node_retrieve)
        graph.add_node("assess", self._node_assess)
        graph.add_node("reformulate", self._node_reformulate)
        # Node ids must not collide with state keys ("answer", "critique"),
        # which is why these are named for the action rather than the field.
        graph.add_node("generate", self._node_answer)
        graph.add_node("critique_answer", self._node_critique)
        graph.add_node("validate", self._node_validate)

        graph.add_edge(START, "tool_router")
        graph.add_edge("tool_router", "retrieve")
        graph.add_edge("retrieve", "assess")
        graph.add_conditional_edges(
            "assess", self._route_after_assess, {"answer": "generate", "reformulate": "reformulate"}
        )
        graph.add_edge("reformulate", "retrieve")
        graph.add_edge("generate", "critique_answer")
        graph.add_edge("critique_answer", "validate")
        graph.add_edge("validate", END)
        return graph.compile()

    # -- entrypoint -------------------------------------------------------
    async def run(
        self,
        question: str,
        *,
        collection: str = "documents",
        top_k: int | None = None,
        session_id: str | None = None,
        self_critique: bool = True,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        memory = ConversationMemory(session_id) if session_id else None
        turns: list[Turn] = await memory.turns() if memory else []
        # A follow-up like "does that apply to annual plans?" is meaningless to a
        # retriever, so resolve it against the previous turn before searching.
        retrieval_query = resolve_followup(question, turns)
        conversation_context = await memory.as_context() if memory else ""

        initial: GraphState = {
            "question": question,
            "current_query": retrieval_query,
            "collection": collection,
            "top_k": top_k,
            "self_critique": self_critique,
            "owner_id": owner_id,
            "iterations": 0,
            "reformulated_queries": [],
            "tool_invocations": [],
            "conversation_context": conversation_context,
            "trace": [],
        }

        final: GraphState = await self.graph.ainvoke(initial)

        if memory and final.get("found"):
            await memory.append(question, final.get("answer", ""))

        groundedness = final.get("groundedness") or {}
        self.telemetry.annotate_last(
            retrieval_loops=final.get("iterations", 1),
            # Persisted per request so /observability/summary reports measured
            # quality alongside cost and latency, not just the CI gate's number.
            faithfulness=groundedness.get("score"),
            answer_relevance=groundedness.get("answer_relevance"),
        )
        return {
            "question": question,
            "answer": final.get("answer", NOT_FOUND),
            "found": bool(final.get("found")),
            "chunks": final.get("chunks", []),
            # An abstention cites nothing: showing sources next to "I could not
            # find that" implies support the answer explicitly does not have.
            "citations": (
                [c.model_dump() for c in build_citations(final.get("chunks", []), self.settings)]
                if final.get("found")
                else []
            ),
            "retrieval_loops": final.get("iterations", 1),
            "reformulated_queries": final.get("reformulated_queries", []),
            "groundedness": final.get("groundedness"),
            "critique": final.get("critique"),
            "tool_invocations": final.get("tool_invocations", []),
            "trace": final.get("trace", []),
            "retrieval_meta": final.get("retrieval_meta", {}),
        }


def build_citations(chunks: list[dict[str, Any]], settings: Settings | None = None) -> list[Citation]:
    """One citation per source document, in retrieval order."""
    settings = settings or get_settings()
    citations: list[Citation] = []
    seen: set[str] = set()
    for chunk in chunks:
        if chunk["score"] < settings.min_retrieval_score:
            continue
        doc_id = chunk.get("document_id") or chunk.get("chunk_id", "")
        if doc_id in seen:
            continue
        seen.add(doc_id)
        snippet = chunk.get("text", "")
        citations.append(
            Citation(
                index=len(citations) + 1,
                document_id=doc_id,
                document_title=chunk.get("document_title"),
                source=chunk.get("source"),
                snippet=snippet[:280] + ("..." if len(snippet) > 280 else ""),
            )
        )
    return citations
