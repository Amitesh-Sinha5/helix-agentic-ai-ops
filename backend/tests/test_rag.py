"""Phase 4 + 12: hybrid retrieval, the agentic loop, tools, memory, feedback."""

from __future__ import annotations

import httpx

from app.core.llm_client import LLMClient
from app.core.telemetry import Telemetry, TracedLLM
from app.rag.agents import NOT_FOUND, DocQAPipeline
from app.rag.ingest import chunk_text
from app.rag.memory import is_followup, resolve_followup
from app.rag.retrieval import Candidate, hybrid_search, reciprocal_rank_fusion
from app.rag.vectorstore import StoredChunk


def trace_nodes(payload: dict) -> list[str]:
    """Node names of completed steps (each node emits a start and a finish)."""
    return [e["node"] for e in payload["trace"] if e["phase"] == "finish"]


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def test_chunking_respects_size_and_overlaps():
    text = " ".join(f"Sentence number {i} carries some content." for i in range(200))
    chunks = chunk_text(text, chunk_size=300, overlap=60)

    assert len(chunks) > 1
    assert all(len(c.text) <= 400 for c in chunks), "chunks should stay near the target size"
    # Overlap means consecutive chunks share vocabulary rather than cutting clean.
    first_tail = set(chunks[0].text.split()[-8:])
    second_head = set(chunks[1].text.split()[:12])
    assert first_tail & second_head


def test_short_text_is_a_single_chunk():
    chunks = chunk_text("Just one short sentence.", chunk_size=800)
    assert len(chunks) == 1


def test_empty_text_produces_no_chunks():
    assert chunk_text("   \n  ") == []


# --------------------------------------------------------------------------- #
# Fusion
# --------------------------------------------------------------------------- #


def _chunk(cid: str) -> StoredChunk:
    return StoredChunk(chunk_id=cid, document_id="doc1", text=f"text for {cid}")


def test_rrf_rewards_agreement_between_retrievers():
    """A chunk both retrievers rank highly must beat one that only one found."""
    vector = [_chunk("a"), _chunk("b"), _chunk("c")]
    keyword = [(_chunk("c"), 9.0), (_chunk("a"), 3.0)]

    fused = reciprocal_rank_fusion(vector, keyword, k=60)
    ids = [c.chunk.chunk_id for c in fused]

    assert ids[0] == "a", "ranked #1 by vector and #2 by keyword should win"
    assert set(ids) == {"a", "b", "c"}
    a = next(c for c in fused if c.chunk.chunk_id == "a")
    assert a.vector_rank == 1 and a.keyword_rank == 2
    b = next(c for c in fused if c.chunk.chunk_id == "b")
    assert a.fused_score > b.fused_score


def test_rrf_keeps_keyword_only_results():
    """A rare literal token found only by BM25 must survive fusion."""
    fused = reciprocal_rank_fusion([], [(_chunk("only-bm25"), 12.0)], k=60)
    assert [c.chunk.chunk_id for c in fused] == ["only-bm25"]
    assert fused[0].vector_rank is None and fused[0].keyword_rank == 1


def test_final_score_prefers_rerank_when_present():
    candidate = Candidate(chunk=_chunk("a"), fused_score=0.01)
    assert candidate.final_score == 0.01
    candidate.rerank_score = 0.87
    assert candidate.final_score == 0.87


# --------------------------------------------------------------------------- #
# Retrieval integration
# --------------------------------------------------------------------------- #


async def test_hybrid_search_uses_both_retrievers_and_reranks(user_client, ingested_policy):
    client = LLMClient()
    embedding = await client.embed_one("How long is the free trial period?")
    result = await hybrid_search("How long is the free trial period?", embedding=embedding)

    assert result.vector_hits > 0, "vector search returned nothing"
    assert result.keyword_hits > 0, "BM25 returned nothing"
    assert result.fused_hits >= max(result.vector_hits, result.keyword_hits)
    assert result.candidates, "reranking dropped everything"
    assert all(c.rerank_score is not None for c in result.candidates)
    assert "14 days" in " ".join(c.chunk.text for c in result.candidates)
    assert result.timings_ms["search_ms"] >= 0


# --------------------------------------------------------------------------- #
# The agent graph
# --------------------------------------------------------------------------- #


async def test_grounded_answer_via_hybrid_search(user_client: httpx.AsyncClient, ingested_policy):
    response = await user_client.post(
        "/docs/query", json={"question": "How long does the free trial last?", "use_cache": False}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["found"] is True
    assert "14 days" in body["answer"]
    assert body["citations"], "a grounded answer must cite its sources"
    assert body["citations"][0]["document_id"] == ingested_policy["document_id"]
    assert body["groundedness"]["grounded"] is True
    assert body["usage"]["llm_calls"] > 0
    assert body["usage"]["cost_usd"] > 0

    nodes = trace_nodes(body)
    for expected in ("retriever", "context_check", "answer", "validator"):
        assert expected in nodes, f"{expected} missing from trace: {nodes}"


async def test_out_of_scope_question_returns_an_honest_not_found(
    user_client: httpx.AsyncClient, ingested_policy
):
    response = await user_client.post(
        "/docs/query",
        json={
            "question": "What is the recommended nitrogen mix for scuba diving below 40 metres?",
            "use_cache": False,
        },
    )
    body = response.json()

    assert body["found"] is False
    assert body["answer"] == NOT_FOUND
    assert body["citations"] == []


async def test_vague_question_triggers_the_requery_loop(user_client: httpx.AsyncClient, ingested_policy):
    """The graph must reformulate and retrieve again rather than answer badly."""
    telemetry = Telemetry(pod="doc_qa", request_id="loop-test")
    pipeline = DocQAPipeline(TracedLLM(telemetry), telemetry)

    result = await pipeline.run(
        "What are the caps?",  # deliberately underspecified
        collection="documents",
    )

    assert result["retrieval_loops"] == 2, "expected exactly one re-query"
    assert result["reformulated_queries"], "no reformulated query was recorded"
    nodes = [e["node"] for e in result["trace"] if e["phase"] == "finish"]
    assert nodes.count("retriever") == 2
    assert "reformulate" in nodes

    reformulation = next(e for e in result["trace"] if e["node"] == "reformulate" and e["phase"] == "finish")
    assert reformulation["detail"]["query"] != "What are the caps?"


async def test_retrieval_loop_is_bounded(user_client: httpx.AsyncClient, ingested_policy):
    """An unanswerable question must stop at the budget, not loop forever."""
    telemetry = Telemetry(pod="doc_qa", request_id="bounded")
    pipeline = DocQAPipeline(TracedLLM(telemetry), telemetry)
    result = await pipeline.run("Explain quantum chromodynamics gluon confinement", collection="documents")

    assert result["retrieval_loops"] <= pipeline.settings.max_retrieval_loops
    assert result["found"] is False


async def test_query_against_empty_collection_abstains(user_client: httpx.AsyncClient):
    response = await user_client.post(
        "/docs/query", json={"question": "Anything at all?", "collection": "nothing-here"}
    )
    assert response.status_code == 200
    assert response.json()["found"] is False


async def test_query_requires_authentication(client: httpx.AsyncClient):
    assert (await client.post("/docs/query", json={"question": "hello there"})).status_code == 401


# --------------------------------------------------------------------------- #
# Ingestion endpoints
# --------------------------------------------------------------------------- #


async def test_ingest_returns_chunk_counts_and_lists_the_document(
    user_client: httpx.AsyncClient, ingested_policy
):
    assert ingested_policy["chunk_count"] > 0
    assert ingested_policy["char_count"] > 0

    listing = await user_client.get("/docs/documents")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1


async def test_reingest_replaces_chunks_and_invalidates_cache(user_client: httpx.AsyncClient):
    payload = {"title": "Policy", "text": "The refund window is 30 days.", "collection": "documents"}
    first = await user_client.post("/docs/ingest", json=payload)
    assert first.json()["reingested"] is False

    await user_client.post("/docs/query", json={"question": "What is the refund window?"})

    payload["text"] = "The refund window is 45 days."
    second = await user_client.post("/docs/ingest", json=payload)
    assert second.json()["reingested"] is True
    assert second.json()["document_id"] == first.json()["document_id"]

    # The stale cached answer must not survive the re-ingest.
    fresh = await user_client.post("/docs/query", json={"question": "What is the refund window?"})
    assert "45 days" in fresh.json()["answer"]


async def test_delete_document_removes_it_from_retrieval(user_client: httpx.AsyncClient, ingested_policy):
    deleted = await user_client.delete(f"/docs/documents/{ingested_policy['document_id']}")
    assert deleted.status_code == 200
    assert deleted.json()["chunks_removed"] > 0

    response = await user_client.post(
        "/docs/query", json={"question": "How long does the free trial last?", "use_cache": False}
    )
    assert response.json()["found"] is False


async def test_cannot_delete_another_users_document(client: httpx.AsyncClient, user_client, ingested_policy):
    other = await client.post(
        "/auth/signup", json={"email": "intruder@helix.example.com", "password": "passw0rd1"}
    )
    token = other.json()["tokens"]["access_token"]
    response = await client.delete(
        f"/docs/documents/{ingested_policy['document_id']}", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Phase 12: tools, memory, self-critique, feedback
# --------------------------------------------------------------------------- #


async def test_tool_calling_fires_for_an_order_status_question(
    user_client: httpx.AsyncClient, ingested_policy
):
    response = await user_client.post(
        "/docs/query", json={"question": "What is the status of my order ORD1042?", "use_cache": False}
    )
    body = response.json()

    assert body["tool_invocations"], "expected the order-status tool to be invoked"
    invocation = body["tool_invocations"][0]
    assert invocation["tool"] == "lookup_order_status"
    assert invocation["arguments"]["order_id"] == "ORD1042"
    assert "tool_router" in trace_nodes(body)


async def test_tool_is_not_called_for_an_ordinary_question(user_client: httpx.AsyncClient, ingested_policy):
    response = await user_client.post(
        "/docs/query", json={"question": "How long does the free trial last?", "use_cache": False}
    )
    assert response.json()["tool_invocations"] == []


def test_followup_detection_and_resolution():
    assert is_followup("Does that apply to annual plans?")
    assert not is_followup("What is the data retention period for uploaded documents in cold storage?")

    from app.rag.memory import Turn

    turns = [Turn(question="What is the refund window?", answer="30 days.")]
    resolved = resolve_followup("Does that apply to annual plans?", turns)
    assert "refund" in resolved and "window" in resolved


async def test_conversation_memory_carries_context_across_turns(
    user_client: httpx.AsyncClient, ingested_policy
):
    first = await user_client.post(
        "/docs/query",
        json={"question": "How long is the free trial?", "session_id": "sess-1", "use_cache": False},
    )
    assert first.json()["found"] is True

    followup = await user_client.post(
        "/docs/query",
        json={"question": "Is a card required for it?", "session_id": "sess-1", "use_cache": False},
    )
    body = followup.json()
    assert body["found"] is True
    assert "credit card" in body["answer"].lower() or "trial" in body["answer"].lower()


async def test_self_critique_node_runs_and_can_be_disabled(user_client: httpx.AsyncClient, ingested_policy):
    with_critique = await user_client.post(
        "/docs/query",
        json={"question": "How long does the free trial last?", "self_critique": True, "use_cache": False},
    )
    assert "self_critique" in trace_nodes(with_critique.json())
    assert with_critique.json()["critique"]

    without = await user_client.post(
        "/docs/query",
        json={"question": "How long does the free trial last?", "self_critique": False, "use_cache": False},
    )
    assert "self_critique" not in trace_nodes(without.json())


async def test_feedback_is_recorded(user_client: httpx.AsyncClient, ingested_policy):
    query = await user_client.post(
        "/docs/query", json={"question": "How long does the free trial last?", "use_cache": False}
    )
    body = query.json()

    response = await user_client.post(
        "/docs/feedback",
        json={
            "request_id": body["request_id"],
            "rating": -1,
            "question": body["question"],
            "answer": body["answer"],
            "comment": "Missed the annual plan case",
        },
    )
    assert response.status_code == 201
    assert response.json()["rating"] == -1


async def test_answers_are_pii_redacted(user_client: httpx.AsyncClient):
    await user_client.post(
        "/docs/ingest",
        json={
            "title": "Contacts",
            "text": (
                "Escalation contacts for billing disputes. "
                "For billing disputes contact billing-team@acme-corp.com or call 415-555-0134 "
                "and quote your dispute reference."
            ),
        },
    )
    response = await user_client.post(
        "/docs/query", json={"question": "Who do I contact about billing disputes?", "use_cache": False}
    )
    answer = response.json()["answer"]
    assert "billing-team@acme-corp.com" not in answer
    assert "REDACTED" in answer


# --------------------------------------------------------------------------- #
# Tenant isolation
# --------------------------------------------------------------------------- #


async def test_retrieval_never_crosses_users(client: httpx.AsyncClient, user_client, ingested_policy):
    """One user must not be able to retrieve another user's document text.

    The owner check on the document *listing* is not enough on its own: the
    vector index and the BM25 index both span the whole collection, so both
    retrievers have to be scoped or the content leaks straight back out.
    """
    intruder = await client.post(
        "/auth/signup", json={"email": "tenant-b@helix.example.com", "password": "passw0rd1"}
    )
    headers = {"Authorization": f"Bearer {intruder.json()['tokens']['access_token']}"}

    # A question that is answerable *only* from the other user's document.
    response = await client.post(
        "/docs/query",
        json={"question": "How long does the free trial last?", "use_cache": False},
        headers=headers,
    )
    body = response.json()

    assert body["found"] is False, f"leaked another user's document: {body['answer']!r}"
    assert "14 days" not in body["answer"]
    assert body["citations"] == []
    assert body["chunks"] == [], "another user's chunks were retrieved"

    # The owner still gets their answer.
    owner = await user_client.post(
        "/docs/query", json={"question": "How long does the free trial last?", "use_cache": False}
    )
    assert "14 days" in owner.json()["answer"]


async def test_cached_answers_are_not_shared_between_users(
    client: httpx.AsyncClient, user_client, ingested_policy
):
    """A cache keyed only by collection would hand user A's answer to user B."""
    question = {"question": "How long does the free trial last?"}

    first = await user_client.post("/docs/query", json=question)
    assert "14 days" in first.json()["answer"]

    intruder = await client.post(
        "/auth/signup", json={"email": "tenant-c@helix.example.com", "password": "passw0rd1"}
    )
    headers = {"Authorization": f"Bearer {intruder.json()['tokens']['access_token']}"}

    response = await client.post("/docs/query", json=question, headers=headers)
    assert response.headers["X-Cache"] == "MISS", "served another user's cached answer"
    assert response.json()["found"] is False


async def test_support_kb_retrieval_is_scoped_to_the_owner(
    client: httpx.AsyncClient, user_client, ingested_kb
):
    intruder = await client.post(
        "/auth/signup", json={"email": "tenant-d@helix.example.com", "password": "passw0rd1"}
    )
    headers = {"Authorization": f"Bearer {intruder.json()['tokens']['access_token']}"}

    response = await client.post(
        "/support/triage",
        json={"subject": "Duplicate charge", "body": "I was charged twice, please refund."},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["kb_sources"] == [], "leaked another user's knowledge base"
