"""Prompt construction.

Every prompt in Helix is a set of UPPERCASE-titled sections:

    QUESTION:
    How long is the trial?

    CONTEXT:
    [1] ...

That structure is not cosmetic -- it is a contract. Real providers get a clearly
delimited prompt, and `MockBrain._section` parses the very same sections back
out, which is what lets the mock provider behave like a model that actually read
its input instead of returning a fixed string.
"""

from __future__ import annotations

SECTION_ORDER_HINT = "Sections are uppercase headers followed by a colon and a newline."


def build_prompt(**sections: object) -> str:
    """Render keyword sections into the canonical prompt format.

    Keys become uppercase headers with underscores turned into spaces; falsy
    values are omitted so optional sections never leave an empty block behind.
    """
    blocks: list[str] = []
    for key, value in sections.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        header = key.replace("_", " ").upper()
        blocks.append(f"{header}:\n{str(value).strip()}")
    return "\n\n".join(blocks)


def format_context(chunks: list[dict]) -> str:
    """Render retrieved chunks as numbered, citable context."""
    lines = []
    for i, chunk in enumerate(chunks, start=1):
        source = chunk.get("source") or chunk.get("document_title") or "document"
        lines.append(f"[{i}] (source: {source})\n{chunk.get('text', '').strip()}")
    return "\n\n".join(lines)


# --------------------------------------------------------------------------- #
# System prompts
# --------------------------------------------------------------------------- #

RAG_ANSWER_SYSTEM = (
    "You are a precise document question-answering assistant. Answer ONLY from the "
    "CONTEXT section. Cite the bracketed source numbers you used. If the context does "
    "not contain the answer, reply exactly: "
    "'I could not find that in the provided documents.' Never speculate."
)

CONTEXT_SUFFICIENCY_SYSTEM = (
    "You judge whether retrieved context is sufficient to answer a question. "
    'Reply with JSON: {"sufficient": bool, "confidence": float, "reason": str, '
    '"missing_information": [str]}. No prose outside the JSON.'
)

QUERY_REFORMULATION_SYSTEM = (
    "You rewrite a search query that failed to retrieve sufficient context. Produce a "
    "broader query using synonyms and domain terms covering the missing information. "
    'Reply with JSON: {"query": str, "rationale": str}.'
)

GROUNDEDNESS_SYSTEM = (
    "You verify that an answer is fully supported by the provided context and that it "
    "addresses the question asked. "
    'Reply with JSON: {"grounded": bool, "score": float, "reason": str, '
    '"answer_relevance": float}, where score is the fraction of the answer\'s claims '
    "supported by the context and answer_relevance is how directly the answer responds "
    "to the question. Both are 0.0-1.0."
)

SELF_CRITIQUE_SYSTEM = (
    "You are a strict reviewer of a draft answer. Remove any claim not supported by the "
    "context, tighten the wording, and keep every citation. "
    'Reply with JSON: {"revised_answer": str, "changed": bool, "critique": str}.'
)

CODE_QUALITY_SYSTEM = (
    "You are a senior engineer reviewing code for correctness, readability, error "
    "handling and maintainability. Report only real, actionable issues. Reply with JSON: "
    '{"issues": [{"severity": "critical|high|medium|low", "category": str, "line": int, '
    '"title": str, "explanation": str, "suggestion": str}]}.'
)

CODE_SECURITY_SYSTEM = (
    "You are an application security reviewer. Look for injection, unsafe "
    "deserialisation, hardcoded secrets, weak cryptography, unsafe subprocess use and "
    "disabled TLS verification. Reply with the same JSON issue schema as the quality "
    "reviewer, using category 'security'."
)

CODE_SUMMARY_SYSTEM = (
    "You merge code-review findings into a verdict. Reply with JSON: "
    '{"verdict": "approve|comment|request_changes", "summary": str, '
    '"severity_counts": object, "top_recommendation": str|null}.'
)

TICKET_CLASSIFICATION_SYSTEM = (
    "You classify a support ticket. Reply with JSON: "
    '{"priority": "urgent|high|medium|low", "category": "billing|bug|account|how_to|'
    'feature_request|general", "confidence": float, "reason": str}.'
)

SUPPORT_DRAFT_SYSTEM = (
    "You draft a support reply grounded strictly in the KNOWLEDGE BASE section. Be warm, "
    "concise and specific. Never invent policy, prices or timelines. Plain text only."
)

ESCALATION_SYSTEM = (
    "You decide whether a support ticket needs human escalation. Escalate for outages, "
    "data loss, security or legal exposure, churn risk, or urgent priority. "
    'Reply with JSON: {"escalate": bool, "reason": str, "suggested_owner": str}.'
)

JUDGE_SYSTEM = (
    "You are an impartial evaluator scoring one metric on a 0.0-1.0 scale. "
    "- faithfulness: fraction of the ANSWER supported by the CONTEXT. "
    "- answer_relevance: how directly the ANSWER addresses the QUESTION. "
    "- context_precision: whether the CONTEXT contains what the GROUND TRUTH needs. "
    'Reply with JSON: {"score": float, "metric": str}.'
)

TOOL_ROUTER_SYSTEM = (
    "You decide whether a question needs an external tool before answering. "
    "Available tool: lookup_order_status(order_id). "
    'Reply with JSON: {"tool": str|null, "arguments": object}.'
)
