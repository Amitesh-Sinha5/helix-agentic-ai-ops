"""Doc Q&A endpoints: ingest, query, list documents, feedback."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status
from sqlalchemy import func, select

from app.config import get_settings
from app.core.cache import get_cache
from app.core.deps import CurrentUser, DBSession, make_telemetry
from app.core.errors import NotFound, PayloadTooLarge
from app.core.telemetry import Telemetry, TracedLLM
from app.db.models import Document, Feedback
from app.rag.agents import DocQAPipeline
from app.rag.ingest import Ingestor, content_hash, extract_text
from app.rag.retrieval import invalidate_keyword_index
from app.schemas.common import Page, TraceEvent, UsageStats
from app.schemas.rag import (
    Citation,
    DocumentOut,
    FeedbackRequest,
    FeedbackResponse,
    GroundednessVerdict,
    IngestRequest,
    IngestResponse,
    QueryRequest,
    QueryResponse,
    RetrievedChunk,
)

logger = logging.getLogger("helix.rag.router")

router = APIRouter(prefix="/docs", tags=["doc-qa"])

PodTelemetry = Annotated[Telemetry, Depends(make_telemetry("doc_qa"))]

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


async def _persist_document(
    db: DBSession,
    *,
    user_id: str,
    title: str,
    text: str,
    source: str | None,
    collection: str,
    content_type: str,
    metadata: dict,
) -> tuple[Document, bool]:
    """Upsert by (owner, title) so re-uploading a document replaces it."""
    digest = content_hash(text)
    existing = await db.execute(
        select(Document).where(
            Document.owner_id == user_id, Document.title == title, Document.collection == collection
        )
    )
    document = existing.scalar_one_or_none()
    if document is not None:
        document.content_hash = digest
        document.source = source
        document.char_count = len(text)
        document.doc_metadata = metadata
        return document, True

    document = Document(
        owner_id=user_id,
        title=title,
        source=source,
        collection=collection,
        content_type=content_type,
        content_hash=digest,
        char_count=len(text),
        doc_metadata=metadata,
    )
    db.add(document)
    await db.flush()
    return document, False


@router.post("/ingest", response_model=IngestResponse, status_code=status.HTTP_201_CREATED)
async def ingest_document(
    payload: IngestRequest, user: CurrentUser, db: DBSession, telemetry: PodTelemetry
) -> IngestResponse:
    """Chunk, embed and index a document, then invalidate stale cached answers."""
    llm = TracedLLM(telemetry)
    document, replaced = await _persist_document(
        db,
        user_id=user.id,
        title=payload.title,
        text=payload.text,
        source=payload.source,
        collection=payload.collection,
        content_type="text/plain",
        metadata=payload.metadata,
    )

    result = await Ingestor().ingest(
        document_id=document.id,
        title=payload.title,
        text=payload.text,
        embedder=llm,
        collection=payload.collection,
        source=payload.source,
        metadata=payload.metadata,
        owner_id=user.id,
        replace=replaced,
    )
    document.chunk_count = result.chunk_count
    document.status = "ready"
    invalidate_keyword_index(payload.collection)
    await telemetry.flush(db)

    return IngestResponse(
        document_id=document.id,
        title=document.title,
        chunk_count=result.chunk_count,
        char_count=result.char_count,
        collection=payload.collection,
        reingested=result.reingested,
        cache_invalidated=result.cache_invalidated,
    )


@router.post("/ingest/file", response_model=IngestResponse, status_code=status.HTTP_201_CREATED)
async def ingest_file(
    user: CurrentUser,
    db: DBSession,
    telemetry: PodTelemetry,
    file: Annotated[UploadFile, File(description="A .txt, .md or .pdf document")],
    title: Annotated[str | None, Form()] = None,
    collection: Annotated[str, Form()] = "documents",
) -> IngestResponse:
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise PayloadTooLarge(f"File exceeds the {MAX_UPLOAD_BYTES // 1024 // 1024}MB limit")
    text = extract_text(data, file.content_type or "text/plain", file.filename)
    if not text.strip():
        raise ValueError("No extractable text found in the uploaded file")

    return await ingest_document(
        IngestRequest(
            title=title or file.filename or "Untitled",
            text=text,
            source=file.filename,
            collection=collection,
        ),
        user,
        db,
        telemetry,
    )


@router.post("/query", response_model=QueryResponse)
async def query_documents(
    payload: QueryRequest, user: CurrentUser, db: DBSession, telemetry: PodTelemetry, response: Response
) -> QueryResponse:
    """Answer a question against the indexed corpus.

    A semantic cache sits in front of the whole agent graph: if a sufficiently
    similar question has been answered recently, that answer is returned without
    a single LLM call.
    """
    settings = get_settings()
    llm = TracedLLM(telemetry)
    cache = get_cache()
    # Scoped per user as well as per collection: a shared namespace would let
    # one user receive an answer generated from another user's documents.
    namespace = f"docqa:{payload.collection}:{user.id}"
    response.headers["X-Request-ID"] = telemetry.request_id

    # Follow-ups depend on session history, so they must never serve a cache
    # entry created in another conversation.
    cacheable = payload.use_cache and settings.semantic_cache_enabled and not payload.session_id

    question_embedding: list[float] | None = None
    if cacheable:
        question_embedding = await llm.embed_one(payload.question, operation="rag.embed_cache_probe")
        try:
            hit = await cache.semantic_lookup(namespace, payload.question, question_embedding)
        except Exception:
            logger.warning("semantic cache lookup failed", exc_info=True)
            hit = None
        if hit is not None:
            telemetry.record_cache_hit(
                "rag.query", similarity=hit.similarity, saved_cost_usd=hit.payload.get("cost_usd", 0.0)
            )
            await telemetry.flush(db)
            cached = hit.payload
            response.headers["X-Cache"] = "HIT"
            return QueryResponse(
                request_id=telemetry.request_id,
                question=payload.question,
                answer=cached["answer"],
                found=cached.get("found", True),
                citations=[Citation(**c) for c in cached.get("citations", [])],
                chunks=[RetrievedChunk(**c) for c in cached.get("chunks", [])],
                retrieval_loops=cached.get("retrieval_loops", 1),
                reformulated_queries=cached.get("reformulated_queries", []),
                groundedness=(
                    GroundednessVerdict(**cached["groundedness"]) if cached.get("groundedness") else None
                ),
                critique=cached.get("critique"),
                trace=[
                    TraceEvent(
                        request_id=telemetry.request_id,
                        pod="doc_qa",
                        node="semantic_cache",
                        phase="finish",
                        sequence=1,
                        duration_ms=0.0,
                        message=(
                            f"Cache hit ({'exact' if hit.exact else f'{hit.similarity:.3f} similarity'}) "
                            f"- answered with zero LLM calls"
                        ),
                        detail={
                            "similarity": round(hit.similarity, 4),
                            "exact": hit.exact,
                            "cached_question": hit.cached_question,
                        },
                    )
                ],
                usage=UsageStats(
                    request_id=telemetry.request_id,
                    latency_ms=round(telemetry.elapsed_ms, 2),
                    llm_calls=0,
                    total_tokens=0,
                    cost_usd=0.0,
                    cache_hit=True,
                ),
            )

    result = await DocQAPipeline(llm, telemetry).run(
        payload.question,
        collection=payload.collection,
        top_k=payload.top_k,
        session_id=payload.session_id,
        self_critique=payload.self_critique,
        owner_id=user.id,
    )
    response.headers["X-Cache"] = "MISS"

    if cacheable and result["found"]:
        try:
            await cache.semantic_store(
                namespace,
                payload.question,
                question_embedding or await llm.embed_one(payload.question),
                {
                    "answer": result["answer"],
                    "found": result["found"],
                    "citations": result["citations"],
                    "chunks": result["chunks"],
                    "retrieval_loops": result["retrieval_loops"],
                    "reformulated_queries": result["reformulated_queries"],
                    "groundedness": result["groundedness"],
                    "critique": result["critique"],
                    "cost_usd": telemetry.total_cost_usd,
                },
            )
        except Exception:
            logger.warning("semantic cache store failed", exc_info=True)

    await telemetry.flush(db)
    return QueryResponse(
        request_id=telemetry.request_id,
        question=payload.question,
        answer=result["answer"],
        found=result["found"],
        citations=[Citation(**c) for c in result["citations"]],
        chunks=[RetrievedChunk(**c) for c in result["chunks"]],
        retrieval_loops=result["retrieval_loops"],
        reformulated_queries=result["reformulated_queries"],
        groundedness=(GroundednessVerdict(**result["groundedness"]) if result["groundedness"] else None),
        critique=result["critique"],
        tool_invocations=result["tool_invocations"],
        trace=[TraceEvent(**e) for e in result["trace"]],
        usage=UsageStats(
            request_id=telemetry.request_id,
            latency_ms=round(telemetry.elapsed_ms, 2),
            llm_calls=telemetry.llm_calls,
            total_tokens=telemetry.total_tokens,
            cost_usd=telemetry.total_cost_usd,
            cache_hit=False,
        ),
    )


@router.get("/documents", response_model=Page[DocumentOut])
async def list_documents(
    user: CurrentUser, db: DBSession, limit: int = 50, offset: int = 0, collection: str | None = None
) -> Page[DocumentOut]:
    conditions = [Document.owner_id == user.id]
    if collection:
        conditions.append(Document.collection == collection)

    total = await db.scalar(select(func.count()).select_from(Document).where(*conditions)) or 0
    result = await db.execute(
        select(Document).where(*conditions).order_by(Document.created_at.desc()).limit(limit).offset(offset)
    )
    return Page[DocumentOut](
        items=[DocumentOut.model_validate(d) for d in result.scalars().all()],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.delete("/documents/{document_id}", status_code=status.HTTP_200_OK)
async def delete_document(document_id: str, user: CurrentUser, db: DBSession) -> dict:
    document = await db.get(Document, document_id)
    if document is None or document.owner_id != user.id:
        raise NotFound(f"Document {document_id} not found")

    from app.rag.vectorstore import get_vector_store

    removed = await get_vector_store().delete_document(document.collection, document_id)
    invalidate_keyword_index(document.collection)
    await get_cache().invalidate_namespace(f"docqa:{document.collection}")
    await db.delete(document)
    return {"deleted": document_id, "chunks_removed": removed}


@router.post("/feedback", response_model=FeedbackResponse, status_code=status.HTTP_201_CREATED)
async def submit_feedback(payload: FeedbackRequest, user: CurrentUser, db: DBSession) -> FeedbackResponse:
    """Capture a thumbs up/down. Downvotes are the seed corpus for golden-set growth."""
    record = Feedback(
        user_id=user.id,
        request_id=payload.request_id,
        pod=payload.pod,
        question=payload.question,
        answer=payload.answer,
        rating=payload.normalised_rating,
        comment=payload.comment,
    )
    db.add(record)
    await db.flush()
    return FeedbackResponse(id=record.id, rating=record.rating)
