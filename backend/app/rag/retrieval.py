"""Hybrid retrieval: vector + BM25, fused with RRF, then cross-encoder reranked.

Why all three stages:

* **Vector search** finds paraphrases but misses rare literal tokens (error
  codes, product SKUs, "SOC 2").
* **BM25** nails those literals but has no notion of synonymy.
* **Reciprocal rank fusion** merges the two ranked lists without needing their
  scores to be on a comparable scale -- which they are not.
* **Reranking** is where most of the quality comes from: a cross-encoder reads
  the query and each candidate *together* rather than comparing two independently
  produced vectors, so it can reject candidates that merely share vocabulary.

The cross-encoder is optional at runtime. If `sentence-transformers` is not
installed (it pulls in torch), retrieval degrades to a lexical reranker instead
of failing -- the pipeline shape stays identical, so nothing downstream changes.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.core.llm_client import content_words, tokenize
from app.rag.vectorstore import StoredChunk, VectorStore, get_vector_store

logger = logging.getLogger("helix.retrieval")


@dataclass
class Candidate:
    chunk: StoredChunk
    vector_rank: int | None = None
    vector_score: float = 0.0
    keyword_rank: int | None = None
    keyword_score: float = 0.0
    fused_score: float = 0.0
    rerank_score: float | None = None

    @property
    def final_score(self) -> float:
        return self.rerank_score if self.rerank_score is not None else self.fused_score

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk.chunk_id,
            "document_id": self.chunk.document_id,
            "text": self.chunk.text,
            "source": self.chunk.source,
            "document_title": self.chunk.document_title,
            "score": round(self.final_score, 6),
            "vector_rank": self.vector_rank,
            "keyword_rank": self.keyword_rank,
            "fused_score": round(self.fused_score, 6),
            "rerank_score": None if self.rerank_score is None else round(self.rerank_score, 6),
        }


@dataclass
class RetrievalResult:
    query: str
    candidates: list[Candidate] = field(default_factory=list)
    vector_hits: int = 0
    keyword_hits: int = 0
    fused_hits: int = 0
    reranker: str = "none"
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def chunks(self) -> list[dict[str, Any]]:
        return [c.as_dict() for c in self.candidates]

    @property
    def top_score(self) -> float:
        return self.candidates[0].final_score if self.candidates else 0.0


# --------------------------------------------------------------------------- #
# BM25 keyword index
# --------------------------------------------------------------------------- #


class BM25Index:
    """Lazily built, generation-cached BM25 index over a collection."""

    def __init__(self) -> None:
        self._indexes: dict[str, tuple[int, Any, list[StoredChunk]]] = {}
        self._lock = asyncio.Lock()

    async def get(self, store: VectorStore, collection: str) -> tuple[Any, list[StoredChunk]]:
        generation = store.generation.get(collection, 0)
        cached = self._indexes.get(collection)
        if cached and cached[0] == generation:
            return cached[1], cached[2]

        async with self._lock:
            cached = self._indexes.get(collection)
            if cached and cached[0] == generation:
                return cached[1], cached[2]
            chunks = await store.all_chunks(collection)
            if not chunks:
                self._indexes[collection] = (generation, None, [])
                return None, []
            # BM25Plus, not BM25Okapi. Okapi's IDF term goes negative for any
            # word appearing in more than half the corpus, and rank_bm25's
            # epsilon floor is computed from the *average* IDF -- which is
            # itself negative on a small corpus, so the floor does not save you.
            # The result is uniformly negative scores. BM25Plus uses
            # log((N+1)/df), which is always positive, so ranking stays sane
            # whether the collection holds three chunks or three thousand.
            from rank_bm25 import BM25Plus

            corpus = [tokenize(c.text) or ["_"] for c in chunks]
            index = await asyncio.to_thread(BM25Plus, corpus)
            self._indexes[collection] = (generation, index, chunks)
            logger.info("Built BM25 index for %s over %d chunks", collection, len(chunks))
            return index, chunks

    def invalidate(self, collection: str | None = None) -> None:
        if collection is None:
            self._indexes.clear()
        else:
            self._indexes.pop(collection, None)


_bm25 = BM25Index()


def _matches(chunk: StoredChunk, where: dict[str, Any] | None) -> bool:
    """Equality-match chunk metadata against a filter.

    Same semantics as the `where` clause passed to Chroma, so both retrievers
    agree on what is visible.
    """
    if not where:
        return True
    metadata = chunk.metadata or {}
    return all(metadata.get(key) == value for key, value in where.items())


async def keyword_search(
    store: VectorStore,
    collection: str,
    query: str,
    *,
    top_k: int,
    where: dict[str, Any] | None = None,
) -> list[tuple[StoredChunk, float]]:
    index, chunks = await _bm25.get(store, collection)
    if index is None or not chunks:
        return []
    tokens = tokenize(query) or ["_"]
    query_terms = set(tokens)
    scores = await asyncio.to_thread(index.get_scores, tokens)

    # BM25Plus gives every document a small floor score even with no term in
    # common, so relevance is decided by genuine lexical overlap rather than by
    # a score threshold. A chunk sharing no query term is not a keyword hit.
    # The BM25 index spans the whole collection, so the ownership filter has to
    # be applied to its results too -- filtering only the vector side would leak
    # the same content straight back through the keyword path.
    matches = [
        (chunk, float(score))
        for chunk, score in zip(chunks, scores, strict=True)
        if query_terms & set(tokenize(chunk.text)) and _matches(chunk, where)
    ]
    matches.sort(key=lambda pair: pair[1], reverse=True)
    return matches[:top_k]


# --------------------------------------------------------------------------- #
# Reciprocal rank fusion
# --------------------------------------------------------------------------- #


def reciprocal_rank_fusion(
    vector_results: list[StoredChunk],
    keyword_results: list[tuple[StoredChunk, float]],
    *,
    k: int = 60,
) -> list[Candidate]:
    """Merge two ranked lists by summing 1/(k + rank).

    Rank-based rather than score-based, because a cosine similarity and a BM25
    score are not on comparable scales and normalising them is guesswork.
    """
    merged: dict[str, Candidate] = {}

    for rank, chunk in enumerate(vector_results, start=1):
        merged[chunk.chunk_id] = Candidate(
            chunk=chunk, vector_rank=rank, vector_score=chunk.score, fused_score=1.0 / (k + rank)
        )

    for rank, (chunk, score) in enumerate(keyword_results, start=1):
        existing = merged.get(chunk.chunk_id)
        if existing is None:
            merged[chunk.chunk_id] = Candidate(
                chunk=chunk, keyword_rank=rank, keyword_score=score, fused_score=1.0 / (k + rank)
            )
        else:
            existing.keyword_rank = rank
            existing.keyword_score = score
            existing.fused_score += 1.0 / (k + rank)

    return sorted(merged.values(), key=lambda c: c.fused_score, reverse=True)


# --------------------------------------------------------------------------- #
# Reranking
# --------------------------------------------------------------------------- #


class LexicalReranker:
    """Fallback reranker used when the cross-encoder is unavailable.

    Scores on query-term coverage, phrase proximity, and a mild length penalty.
    Weaker than a cross-encoder, but it keeps the pipeline shape identical and
    is genuinely better than leaving RRF order untouched.
    """

    name = "lexical"

    def score(self, query: str, texts: list[str]) -> list[float]:
        q_terms = content_words(query)
        if not q_terms:
            return [0.0] * len(texts)
        q_set = set(q_terms)
        scores: list[float] = []
        for text in texts:
            t_tokens = tokenize(text)
            t_set = set(t_tokens)
            coverage = len(q_set & t_set) / len(q_set)

            # Proximity: how tightly the matched query terms cluster together.
            positions = [i for i, tok in enumerate(t_tokens) if tok in q_set]
            proximity = 0.0
            if len(positions) > 1:
                spread = positions[-1] - positions[0] + 1
                proximity = len(positions) / spread
            elif positions:
                proximity = 0.5

            length_penalty = 1.0 / (1.0 + math.log1p(max(0, len(t_tokens) - 120) / 120))
            scores.append(round((0.7 * coverage + 0.3 * proximity) * length_penalty, 6))
        return scores


class CrossEncoderReranker:
    """sentence-transformers cross-encoder, loaded lazily on first use."""

    name = "cross-encoder"

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model: Any | None = None
        self._failed = False

    def available(self) -> bool:
        if self._failed:
            return False
        if self._model is not None:
            return True
        try:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.model_name)
            logger.info("Loaded cross-encoder reranker %s", self.model_name)
            return True
        except Exception as exc:
            logger.warning("Cross-encoder unavailable (%s); using lexical reranker", exc)
            self._failed = True
            return False

    def score(self, query: str, texts: list[str]) -> list[float]:
        assert self._model is not None
        raw = self._model.predict([(query, t) for t in texts])
        # Cross-encoder logits are unbounded; squash to 0-1 for comparability
        # with the lexical fallback and with `min_retrieval_score`.
        return [round(1 / (1 + math.exp(-float(s))), 6) for s in raw]


_lexical = LexicalReranker()
_cross_encoder: CrossEncoderReranker | None = None


def get_reranker() -> Any:
    global _cross_encoder
    settings = get_settings()
    if not settings.reranker_enabled:
        return _lexical
    if _cross_encoder is None:
        _cross_encoder = CrossEncoderReranker(settings.reranker_model)
    return _cross_encoder if _cross_encoder.available() else _lexical


async def rerank(query: str, candidates: list[Candidate], *, top_n: int) -> tuple[list[Candidate], str]:
    if not candidates:
        return [], "none"
    reranker = get_reranker()
    texts = [c.chunk.text for c in candidates]
    scores = await asyncio.to_thread(reranker.score, query, texts)
    for candidate, score in zip(candidates, scores, strict=True):
        candidate.rerank_score = float(score)
    ordered = sorted(candidates, key=lambda c: c.rerank_score or 0.0, reverse=True)
    return ordered[:top_n], reranker.name


# --------------------------------------------------------------------------- #
# The public entrypoint
# --------------------------------------------------------------------------- #


async def hybrid_search(
    query: str,
    *,
    embedding: list[float],
    collection: str = "documents",
    top_k: int | None = None,
    top_n: int | None = None,
    store: VectorStore | None = None,
    where: dict[str, Any] | None = None,
) -> RetrievalResult:
    """Vector + BM25 in parallel -> RRF -> rerank -> top N.

    `where` scopes both retrievers to matching chunk metadata; this is how
    per-owner isolation is enforced.
    """
    settings = get_settings()
    store = store or get_vector_store()
    top_k = top_k or settings.retrieval_top_k
    top_n = top_n or settings.rerank_top_n

    loop = asyncio.get_running_loop()
    started = loop.time()
    vector_results, keyword_results = await asyncio.gather(
        store.query(collection, embedding, top_k=top_k, where=where),
        keyword_search(store, collection, query, top_k=top_k, where=where),
    )
    search_ms = (loop.time() - started) * 1000

    fused = reciprocal_rank_fusion(vector_results, keyword_results, k=settings.rrf_k)

    rerank_started = loop.time()
    top, reranker_name = await rerank(query, fused[: max(top_n * 3, top_n)], top_n=top_n)
    rerank_ms = (loop.time() - rerank_started) * 1000

    return RetrievalResult(
        query=query,
        candidates=top,
        vector_hits=len(vector_results),
        keyword_hits=len(keyword_results),
        fused_hits=len(fused),
        reranker=reranker_name,
        timings_ms={"search_ms": round(search_ms, 2), "rerank_ms": round(rerank_ms, 2)},
    )


def invalidate_keyword_index(collection: str | None = None) -> None:
    _bm25.invalidate(collection)
