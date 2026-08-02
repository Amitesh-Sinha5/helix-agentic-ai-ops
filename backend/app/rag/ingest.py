"""Document ingestion: extract, chunk, embed, upsert, invalidate.

Chunking is sentence-aware with a configurable overlap, so a chunk boundary
rarely lands mid-sentence and adjacent chunks share enough context for a claim
that straddles a boundary to stay retrievable.

Re-ingesting a document is idempotent by content hash and always invalidates the
collection's semantic cache -- otherwise a user who fixes a document would keep
getting the answer generated from the old version.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.core.guardrails import screen_input
from app.rag.vectorstore import VectorStore, get_vector_store

logger = logging.getLogger("helix.ingest")


@dataclass
class Chunk:
    index: int
    text: str
    start: int
    end: int
    metadata: dict[str, Any] = field(default_factory=dict)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_text(data: bytes, content_type: str, filename: str | None = None) -> str:
    """Extract plain text from an uploaded file (PDF, markdown, or text)."""
    name = (filename or "").lower()
    if content_type == "application/pdf" or name.endswith(".pdf"):
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
        except Exception as exc:
            raise ValueError(f"Could not read the PDF: {exc}") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def normalise_whitespace(text: str) -> str:
    """Collapse hard wrapping so a chunk holds whole sentences.

    Documents are commonly wrapped at 80 columns. Left alone, a single newline
    lands mid-sentence and every downstream consumer -- sentence splitting,
    snippet extraction, the quoted answer -- inherits the break. Blank lines are
    preserved, because those are real paragraph boundaries.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Join a line to the next only when it is a continuation, not a new block.
    return re.sub(r"(?<![.!?:;\-\n])\n(?!\n|\s*[-*\d])", " ", text)


def chunk_text(text: str, *, chunk_size: int | None = None, overlap: int | None = None) -> list[Chunk]:
    """Split text into overlapping, sentence-aligned chunks."""
    settings = get_settings()
    chunk_size = chunk_size or settings.chunk_size
    overlap = overlap if overlap is not None else settings.chunk_overlap
    text = normalise_whitespace(text)
    if not text:
        return []
    if len(text) <= chunk_size:
        return [Chunk(index=0, text=text, start=0, end=len(text))]

    # Paragraphs first, then sentences: only fall back to a hard cut for a
    # single sentence longer than the whole chunk budget.
    units: list[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= chunk_size:
            units.append(para)
            continue
        for sentence in _SENTENCE_SPLIT.split(para):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= chunk_size:
                units.append(sentence)
            else:
                for i in range(0, len(sentence), chunk_size):
                    units.append(sentence[i : i + chunk_size])

    chunks: list[Chunk] = []
    buffer: list[str] = []
    buffer_len = 0
    cursor = 0

    def flush() -> None:
        nonlocal buffer, buffer_len, cursor
        if not buffer:
            return
        body = " ".join(buffer).strip()
        start = text.find(buffer[0][:60], cursor)
        start = start if start >= 0 else cursor
        chunks.append(Chunk(index=len(chunks), text=body, start=start, end=start + len(body)))
        cursor = max(cursor, start + max(1, len(body) - overlap))
        # Carry the tail forward so the next chunk overlaps this one.
        carried: list[str] = []
        carried_len = 0
        for unit in reversed(buffer):
            if carried_len + len(unit) > overlap:
                break
            carried.insert(0, unit)
            carried_len += len(unit) + 1
        buffer = carried
        buffer_len = carried_len

    for unit in units:
        if buffer_len + len(unit) + 1 > chunk_size and buffer:
            flush()
        buffer.append(unit)
        buffer_len += len(unit) + 1
    if buffer:
        body = " ".join(buffer).strip()
        if not chunks or chunks[-1].text != body:
            chunks.append(Chunk(index=len(chunks), text=body, start=cursor, end=cursor + len(body)))
    return chunks


@dataclass
class IngestResult:
    document_id: str
    chunk_count: int
    char_count: int
    content_hash: str
    reingested: bool = False
    cache_invalidated: int = 0
    injection_flags: list[str] = field(default_factory=list)


class Ingestor:
    def __init__(self, store: VectorStore | None = None) -> None:
        self.store = store or get_vector_store()

    async def ingest(
        self,
        *,
        document_id: str,
        title: str,
        text: str,
        embedder: Any,
        collection: str = "documents",
        source: str | None = None,
        metadata: dict[str, Any] | None = None,
        owner_id: str | None = None,
        replace: bool = False,
    ) -> IngestResult:
        """Chunk, embed and upsert a document.

        `embedder` is a `TracedLLM` (or `LLMClient`) so embedding cost lands in
        telemetry alongside the rest of the request.
        """
        chunks = chunk_text(text)
        if not chunks:
            raise ValueError("Document contains no indexable text")

        # Ingested content is untrusted: it becomes model context later, so it is
        # screened for prompt injection on the way in and flagged, not silently
        # trusted.
        flags = screen_input(text)
        if flags:
            logger.warning("possible prompt injection in document %s: %s", document_id, flags)

        removed = 0
        if replace:
            removed = await self.store.delete_document(collection, document_id)

        texts = [c.text for c in chunks]
        embeddings = (
            await embedder.embed(texts, operation="embed.ingest")
            if hasattr(embedder, "telemetry")
            else (await embedder.embed(texts))[0]
        )

        base_meta = {
            "document_id": document_id,
            "document_title": title,
            "source": source,
            "collection": collection,
            # Stamped on every chunk so retrieval can scope to the owner.
            # Without it any authenticated user could retrieve the text of any
            # other user's documents: the owner check on the document *listing*
            # does nothing for the vector index.
            "owner_id": owner_id or "",
            **(metadata or {}),
        }
        if flags:
            base_meta["injection_flagged"] = True

        await self.store.upsert(
            collection,
            ids=[f"{document_id}:{c.index}" for c in chunks],
            texts=texts,
            embeddings=embeddings,
            metadatas=[{**base_meta, "chunk_index": c.index} for c in chunks],
        )

        invalidated = await self._invalidate_cache(collection, owner_id)
        return IngestResult(
            document_id=document_id,
            chunk_count=len(chunks),
            char_count=len(text),
            content_hash=content_hash(text),
            reingested=removed > 0,
            cache_invalidated=invalidated,
            injection_flags=flags,
        )

    @staticmethod
    async def _invalidate_cache(collection: str, owner_id: str | None) -> int:
        """Answers cached against the previous version of a collection are stale.

        The namespace is per-owner (see the query router), so invalidation has
        to target the same namespace or a re-ingest would silently keep serving
        answers generated from the old document.
        """
        from app.core.cache import get_cache

        namespace = f"docqa:{collection}:{owner_id}" if owner_id else f"docqa:{collection}"
        try:
            return await get_cache().invalidate_namespace(namespace)
        except Exception:
            logger.warning("cache invalidation failed for %s", namespace, exc_info=True)
            return 0
