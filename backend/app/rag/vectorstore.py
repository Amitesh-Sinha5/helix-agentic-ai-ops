"""Chroma wrapper: embed, upsert, similarity search.

Chroma's client is synchronous, so every call is pushed to a worker thread to
keep the event loop free. Embeddings are computed by `LLMClient` and passed in
explicitly rather than delegated to a Chroma embedding function -- that keeps
one embedding implementation in the codebase and lets telemetry price it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.config import get_settings

logger = logging.getLogger("helix.vectorstore")

# Chroma's bundled posthog client is incompatible with the installed posthog and
# logs an ERROR per call even with anonymized_telemetry off. It is pure noise.
logging.getLogger("chromadb.telemetry.product.posthog").setLevel(logging.CRITICAL)


@dataclass
class StoredChunk:
    chunk_id: str
    document_id: str
    text: str
    source: str | None = None
    document_title: str | None = None
    score: float = 0.0
    metadata: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "text": self.text,
            "source": self.source,
            "document_title": self.document_title,
            "score": self.score,
        }


def _clean_metadata(md: dict[str, Any]) -> dict[str, Any]:
    """Chroma accepts only str/int/float/bool scalars, and rejects None."""
    out: dict[str, Any] = {}
    for key, value in md.items():
        if value is None:
            continue
        out[key] = value if isinstance(value, (str, int, float, bool)) else str(value)
    return out


class VectorStore:
    """Thin async facade over a Chroma client."""

    def __init__(self, persist_dir: str | None = None) -> None:
        settings = get_settings()
        self.persist_dir = persist_dir or settings.chroma_persist_dir
        self._client: Any | None = None
        self._lock = asyncio.Lock()
        # Chroma's get_or_create_collection is not safe to call concurrently for
        # the same name -- hybrid search hits it from two parallel tasks and one
        # loses with UniqueConstraintError. Handles are cached behind a lock and
        # reused, which also saves a round trip per query.
        self._collections: dict[str, Any] = {}
        self._collection_lock = asyncio.Lock()
        # Bumped on every write so BM25 indexes know when to rebuild.
        self.generation: dict[str, int] = {}

    def _build_client(self) -> Any:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        chroma_settings = ChromaSettings(anonymized_telemetry=False, allow_reset=True)
        if self.persist_dir in (":memory:", "", None):
            logger.info("Using ephemeral Chroma client")
            return chromadb.EphemeralClient(settings=chroma_settings)
        return chromadb.PersistentClient(path=self.persist_dir, settings=chroma_settings)

    async def client(self) -> Any:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = await asyncio.to_thread(self._build_client)
        return self._client

    async def _collection(self, name: str) -> Any:
        cached = self._collections.get(name)
        if cached is not None:
            return cached
        async with self._collection_lock:
            if (cached := self._collections.get(name)) is not None:
                return cached
            client = await self.client()
            collection = await asyncio.to_thread(
                client.get_or_create_collection, name=name, metadata={"hnsw:space": "cosine"}
            )
            self._collections[name] = collection
            return collection

    # -- writes -----------------------------------------------------------
    async def upsert(
        self,
        collection: str,
        *,
        ids: list[str],
        texts: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> int:
        if not ids:
            return 0
        col = await self._collection(collection)
        await asyncio.to_thread(
            col.upsert,
            ids=ids,
            documents=texts,
            embeddings=embeddings,
            metadatas=[_clean_metadata(m) for m in metadatas],
        )
        self.generation[collection] = self.generation.get(collection, 0) + 1
        return len(ids)

    async def delete_document(self, collection: str, document_id: str) -> int:
        col = await self._collection(collection)
        existing = await asyncio.to_thread(col.get, where={"document_id": document_id}, include=[])
        ids = existing.get("ids") or []
        if ids:
            await asyncio.to_thread(col.delete, ids=ids)
            self.generation[collection] = self.generation.get(collection, 0) + 1
        return len(ids)

    # -- reads ------------------------------------------------------------
    async def query(
        self, collection: str, embedding: list[float], *, top_k: int = 8, where: dict | None = None
    ) -> list[StoredChunk]:
        col = await self._collection(collection)
        total = await asyncio.to_thread(col.count)
        if total == 0:
            return []
        result = await asyncio.to_thread(
            col.query,
            query_embeddings=[embedding],
            n_results=min(top_k, total),
            where=where or None,
            include=["documents", "metadatas", "distances"],
        )
        return self._to_chunks(result)

    @staticmethod
    def _to_chunks(result: dict) -> list[StoredChunk]:
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        chunks: list[StoredChunk] = []
        for i, chunk_id in enumerate(ids):
            meta = metas[i] or {}
            distance = dists[i] if i < len(dists) else 1.0
            chunks.append(
                StoredChunk(
                    chunk_id=chunk_id,
                    document_id=str(meta.get("document_id", "")),
                    text=docs[i] or "",
                    source=meta.get("source"),
                    document_title=meta.get("document_title"),
                    # Chroma returns cosine *distance*; convert to a similarity.
                    score=round(max(0.0, 1.0 - float(distance)), 6),
                    metadata=meta,
                )
            )
        return chunks

    async def all_chunks(self, collection: str) -> list[StoredChunk]:
        """Every chunk in a collection -- the corpus the BM25 index is built on."""
        col = await self._collection(collection)
        if await asyncio.to_thread(col.count) == 0:
            return []
        result = await asyncio.to_thread(col.get, include=["documents", "metadatas"])
        ids = result.get("ids") or []
        docs = result.get("documents") or []
        metas = result.get("metadatas") or []
        return [
            StoredChunk(
                chunk_id=cid,
                document_id=str((metas[i] or {}).get("document_id", "")),
                text=docs[i] or "",
                source=(metas[i] or {}).get("source"),
                document_title=(metas[i] or {}).get("document_title"),
                metadata=metas[i] or {},
            )
            for i, cid in enumerate(ids)
        ]

    async def count(self, collection: str) -> int:
        col = await self._collection(collection)
        return int(await asyncio.to_thread(col.count))

    async def reset(self) -> None:
        client = await self.client()
        await asyncio.to_thread(client.reset)
        self.generation.clear()
        self._collections.clear()


_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _store
    if _store is None:
        _store = VectorStore()
    return _store


def reset_vector_store() -> None:
    global _store
    _store = None


def clear_chroma_system_cache() -> None:
    """Drop Chroma's process-wide System cache.

    Chroma memoises a System per settings fingerprint and hands the same one to
    every client built from those settings. Without clearing it, a "fresh"
    client in the next test still sees the previous test's collections.
    """
    try:
        from chromadb.api.shared_system_client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
    except Exception:
        logger.debug("could not clear Chroma system cache", exc_info=True)
