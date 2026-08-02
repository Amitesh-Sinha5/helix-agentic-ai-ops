# Roadmap

What Helix does not do yet, and what it would take. Ordered by how much each would actually improve the product.

## Next

### Replace the embedded vector store
Chroma runs in-process against a local directory, which caps horizontal scaling: replicas need a shared `ReadWriteMany` volume, and there is no way to shard or replicate the index. Moving to pgvector (one fewer moving part, since Postgres is already there) or a managed service such as Qdrant would remove the constraint. `app/rag/vectorstore.py` is already the only module that touches Chroma, so this is a single-file swap plus a migration path for existing indexes.

### Streaming answers
The Doc Q&A pod returns a complete answer. With a real model that is several seconds of blank screen, softened but not solved by the live trace. Token streaming over the existing WebSocket would make it feel immediate. The trace channel already exists; the answer node needs to emit deltas instead of one final string.

### Real feedback → evaluation loop
`POST /docs/feedback` captures thumbs up/down into the `feedback` table, and the schema carries a `promoted_to_golden` flag, but nothing consumes it yet. The intended loop: cluster downvoted questions, surface them for review, promote confirmed failures into `tests/golden_dataset.json` so today's bug becomes tomorrow's regression test.

### Organisation-level sharing
Retrieval is scoped per *user*: chunks carry an `owner_id` and both retrievers filter on it, so nothing crosses between accounts. Real teams need a middle ground — documents shared within an organisation but not globally. That means an `organisations` table, membership, and a scope that resolves to a set of owner ids rather than one. The filter plumbing (`owner_scope()` in `app/rag/agents.py`) already takes an arbitrary metadata predicate, so the retrieval side is a small change; the modelling is the work.

## Later

### Model routing by task
Every agent call uses one configured model. Cheap, well-structured steps — sufficiency checks, escalation decisions, tool routing — do not need a frontier model. Routing those to a small model and reserving the large one for answer generation would cut cost substantially. `LLMClient` already takes a `task` per call, so the routing table has a natural home.

### Prompt versioning and A/B evaluation
Prompts live as module constants in `app/core/prompts.py`. Versioning them, recording which version produced each `request_log` row, and scoring versions against the golden dataset would turn prompt changes from guesswork into measurement. The quality gate already provides the scoring machinery.

### Distributed tracing
Telemetry is rich but Helix-shaped. OpenTelemetry spans would let an agent run be followed across service boundaries in a standard tool. `Telemetry.span()` is the obvious adapter point.

### Background job queue
Ingestion is synchronous, so a large PDF blocks its request. Moving chunk-and-embed to a worker (arq or Celery, with the existing Redis) and reporting progress over the existing WebSocket would fix it.

### Admin surface
Escalations are pushed live to admins, but there is no queue to work through them, no assignment, no resolution state. `tickets.status` exists and is never advanced past `triaged`/`escalated`.

## Deliberately not planned

**Fine-tuning a model.** The gap in Doc Q&A quality is retrieval, not generation. Better chunking and reranking would pay off more per hour spent, and the evaluation harness would show it.

**Replacing LangGraph with a hand-rolled state machine.** The graphs are small enough that this is tempting, but conditional edges and the reducer-based state merging in the parallel Code Review fan-out are exactly what LangGraph is good at. Writing it by hand would trade a dependency for a bug surface.

**Multi-region.** Nothing in the current design justifies it. It would add operational cost and consistency problems to solve a problem no one has.
