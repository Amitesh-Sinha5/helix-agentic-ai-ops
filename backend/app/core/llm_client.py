"""A single LLM interface with three interchangeable backends.

    provider=mock       deterministic, offline, zero-cost
    provider=openai     OpenAI chat completions + embeddings
    provider=anthropic  Anthropic messages API

The mock backend is the load-bearing one for tests and CI. It is deliberately
*not* a stub that returns a constant string: each task type has a responder that
reads the prompt and produces a plausible, content-derived answer. The RAG
responder, for instance, answers out of the retrieved context it was handed, so
the Phase 8 faithfulness/groundedness scoring measures something real -- break
retrieval and the mock's answers stop being grounded, and the quality gate goes
red exactly as it would with a live model.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, get_settings

# --------------------------------------------------------------------------- #
# Response envelope
# --------------------------------------------------------------------------- #


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    raw: Any = field(default=None, repr=False)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def json(self) -> Any:
        """Parse the response as JSON, tolerating markdown fences and preamble."""
        return extract_json(self.text)


class LLMError(RuntimeError):
    """Raised when a provider call fails or returns something unusable."""


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction from a model response.

    Handles the three things models actually do: clean JSON, JSON inside a
    ```json fence, and JSON with a sentence of preamble in front of it.
    """
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"Response was not valid JSON: {text[:200]!r}")


def estimate_tokens(text: str) -> int:
    """~4 characters per token. Good enough for cost telemetry, and it keeps the
    mock provider free of a tokenizer dependency."""
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------- #
# Deterministic mock brain
# --------------------------------------------------------------------------- #

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "do",
        "does",
        "can",
        "could",
        "should",
        "would",
        "there",
        "their",
        "this",
        "these",
        "those",
        "about",
        "into",
        "over",
        "under",
        "not",
        "no",
        "if",
        "then",
        "than",
        "but",
        "so",
        "such",
        "our",
        "we",
        "they",
        "he",
        "she",
        "him",
        "her",
        "his",
        "them",
        "us",
        "me",
        "my",
    ]
)


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def content_words(text: str) -> list[str]:
    return [t for t in tokenize(text) if t not in _STOPWORDS and len(t) > 2]


def split_sentences(text: str) -> list[str]:
    """Split on real sentence ends and blank lines only.

    A single newline is *not* a boundary: source documents are routinely hard
    wrapped at 80 columns, and treating those wraps as sentence ends chops
    quoted answers off mid-clause.
    """
    parts = re.split(r"(?<=[.!?])\s+|\n\s*\n", text)
    return [p.strip() for p in parts if len(p.strip()) > 15]


def lexical_overlap(a: str, b: str) -> float:
    """Jaccard-ish overlap over content words, in [0, 1]."""
    wa, wb = set(content_words(a)), set(content_words(b))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def coverage(needle: str, haystack: str) -> float:
    """Fraction of `needle`'s content words that appear in `haystack`.

    Asymmetric on purpose -- this is the shape faithfulness scoring wants
    ("how much of the answer is supported by the context").
    """
    wn, wh = set(content_words(needle)), set(content_words(haystack))
    if not wn:
        return 1.0
    return len(wn & wh) / len(wn)


def hashed_embedding(text: str, dim: int) -> list[float]:
    """Deterministic bag-of-words hashing embedding, L2-normalised.

    Not semantic in the neural sense, but it produces genuine, stable cosine
    similarities driven by shared vocabulary -- enough for the vector store,
    the semantic cache and the retrieval tests to exercise real code paths
    offline. Sub-token shingles give near-miss words partial credit.
    """
    vec = [0.0] * dim
    toks = content_words(text) or tokenize(text) or ["_empty_"]
    for tok in toks:
        for gram in (tok, tok[:4], tok[:6]):
            if not gram:
                continue
            h = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
            idx = h % dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            weight = 1.0 if gram == tok else 0.35
            vec[idx] += sign * weight
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _stable_choice(seed: str, options: list[str]) -> str:
    h = int(hashlib.blake2b(seed.encode(), digest_size=8).hexdigest(), 16)
    return options[h % len(options)]


class MockBrain:
    """Task-aware deterministic responders used by the mock provider."""

    NOT_FOUND = "I could not find that in the provided documents."

    def respond(self, task: str, prompt: str, system: str | None) -> str:
        handler = getattr(self, f"_task_{task}", None)
        if handler is None:
            return self._task_default(prompt, system)
        return handler(prompt, system)

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _section(prompt: str, name: str) -> str:
        """Pull a named block out of a prompt built by app.core.prompts."""
        m = re.search(
            rf"^{re.escape(name)}:\s*\n(.*?)(?=\n[A-Z][A-Za-z ]{{2,30}}:\s*\n|\Z)",
            prompt,
            re.DOTALL | re.MULTILINE,
        )
        return m.group(1).strip() if m else ""

    # -- tasks ------------------------------------------------------------
    def _task_default(self, prompt: str, system: str | None) -> str:
        digest = hashlib.blake2b(prompt.encode(), digest_size=6).hexdigest()
        return f"[mock:{digest}] {prompt.strip()[:180]}"

    def _task_rag_answer(self, prompt: str, system: str | None) -> str:
        """Answer strictly from CONTEXT -- the behaviour we want to measure."""
        question = self._section(prompt, "QUESTION") or prompt
        context = self._section(prompt, "CONTEXT")
        if not context.strip():
            return self.NOT_FOUND
        sentences = split_sentences(context)
        if not sentences:
            return self.NOT_FOUND
        ranked = sorted(sentences, key=lambda s: lexical_overlap(question, s), reverse=True)
        best = ranked[0]
        if lexical_overlap(question, best) < 0.04:
            return self.NOT_FOUND
        answer = best
        if len(ranked) > 1 and lexical_overlap(question, ranked[1]) > 0.08:
            answer = f"{best} {ranked[1]}"
        return answer

    def _task_context_sufficiency(self, prompt: str, system: str | None) -> str:
        question = self._section(prompt, "QUESTION") or prompt
        context = self._section(prompt, "CONTEXT")
        score = max((lexical_overlap(question, s) for s in split_sentences(context)), default=0.0)
        sufficient = score >= 0.10
        missing = [w for w in content_words(question) if w not in set(content_words(context))]
        return json.dumps(
            {
                "sufficient": sufficient,
                "confidence": round(min(1.0, score * 4), 3),
                "reason": (
                    "Retrieved context covers the question terms."
                    if sufficient
                    else f"Context does not cover: {', '.join(missing[:5]) or 'the question topic'}."
                ),
                "missing_information": missing[:5],
            }
        )

    def _task_query_reformulation(self, prompt: str, system: str | None) -> str:
        """Pseudo-relevance feedback: expand the query with the corpus's own
        vocabulary, taken from the chunks the failed pass did retrieve."""
        question = self._section(prompt, "QUESTION") or prompt
        missing = self._section(prompt, "MISSING INFORMATION")
        context = self._section(prompt, "CONTEXT")

        base = content_words(question)
        base_set = set(base)
        # Rank context terms by frequency, keeping the ones the query lacks.
        freq: dict[str, int] = {}
        for word in content_words(context):
            if word not in base_set:
                freq[word] = freq.get(word, 0) + 1
        feedback = [w for w, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:8]]
        extra = [w for w in content_words(missing) if w not in base_set and w not in feedback][:4]

        terms = base + feedback + extra
        seen: list[str] = []
        for t in terms:
            if t not in seen:
                seen.append(t)
        return json.dumps(
            {
                "query": " ".join(seen[:16]),
                "rationale": (
                    "Expanded the query with vocabulary observed in the first pass "
                    f"({', '.join(feedback[:4]) or 'none available'}) plus uncovered terms."
                ),
            }
        )

    def _task_groundedness(self, prompt: str, system: str | None) -> str:
        answer = self._section(prompt, "ANSWER")
        context = self._section(prompt, "CONTEXT")
        question = self._section(prompt, "QUESTION")
        if not answer or answer.strip() == self.NOT_FOUND:
            return json.dumps(
                {"grounded": True, "score": 1.0, "reason": "Abstained.", "answer_relevance": 1.0}
            )
        score = coverage(answer, context)
        relevance = min(1.0, lexical_overlap(question, answer) * 3.0)
        return json.dumps(
            {
                "grounded": score >= 0.6,
                "score": round(score, 3),
                "reason": f"{score:.0%} of the answer's content words appear in the retrieved context.",
                "answer_relevance": round(relevance, 3),
            }
        )

    def _task_self_critique(self, prompt: str, system: str | None) -> str:
        draft = self._section(prompt, "DRAFT ANSWER")
        context = self._section(prompt, "CONTEXT")
        question = self._section(prompt, "QUESTION")
        support = coverage(draft, context)
        addresses = lexical_overlap(question, draft)
        revised = draft.strip()
        issues: list[str] = []
        if support < 0.85:
            unsupported = [w for w in content_words(draft) if w not in set(content_words(context))]
            if unsupported:
                issues.append(f"Unsupported terms trimmed: {', '.join(unsupported[:4])}.")
        if addresses < 0.05:
            issues.append("Draft only loosely addresses the question.")
        return json.dumps(
            {
                "revised_answer": revised,
                "changed": bool(issues),
                "critique": " ".join(issues)
                or "Draft is fully supported by the context; no revision needed.",
            }
        )

    def _task_code_quality(self, prompt: str, system: str | None) -> str:
        code = self._section(prompt, "CODE") or prompt
        issues = []
        for i, line in enumerate(code.splitlines(), start=1):
            stripped = line.strip()
            if len(line) > 100:
                issues.append(
                    {
                        "severity": "low",
                        "category": "style",
                        "line": i,
                        "title": "Line exceeds 100 characters",
                        "explanation": "Long lines hurt readability and review diffs.",
                        "suggestion": "Wrap the expression or extract a local variable.",
                    }
                )
            if re.search(r"\bexcept\s*:", stripped) or "except Exception" in stripped:
                issues.append(
                    {
                        "severity": "medium",
                        "category": "error-handling",
                        "line": i,
                        "title": "Overly broad exception handler",
                        "explanation": "Catching everything hides real failures and complicates debugging.",
                        "suggestion": "Catch the specific exception types you can actually handle.",
                    }
                )
            if re.search(r"\bprint\(", stripped):
                issues.append(
                    {
                        "severity": "low",
                        "category": "observability",
                        "line": i,
                        "title": "print() used instead of logging",
                        "explanation": "print bypasses log levels and structured log collection.",
                        "suggestion": "Use the logging module.",
                    }
                )
            if re.search(r"def \w+\([^)]*=\s*(\[\]|\{\})", stripped):
                issues.append(
                    {
                        "severity": "high",
                        "category": "correctness",
                        "line": i,
                        "title": "Mutable default argument",
                        "explanation": "The default is created once and shared across every call.",
                        "suggestion": "Default to None and build the container inside the function.",
                    }
                )
            if "TODO" in stripped or "FIXME" in stripped:
                issues.append(
                    {
                        "severity": "low",
                        "category": "maintainability",
                        "line": i,
                        "title": "Unresolved TODO/FIXME",
                        "explanation": "Unresolved markers tend to outlive the code they annotate.",
                        "suggestion": "Resolve it or link a tracked issue.",
                    }
                )
        return json.dumps({"issues": issues[:15]})

    def _task_code_security(self, prompt: str, system: str | None) -> str:
        code = self._section(prompt, "CODE") or prompt
        patterns = [
            (
                r"\beval\s*\(|\bexec\s*\(",
                "critical",
                "Arbitrary code execution via eval/exec",
                "eval/exec on any attacker-influenced value is remote code execution.",
                "Parse the input explicitly, or use ast.literal_eval for literals.",
            ),
            (
                # Two shapes, because real code separates them: a SQL statement
                # built by interpolation on one line, and execute() called with
                # an interpolated argument on another.
                r"(?is)\b(?:select|insert|update|delete)\b.*?(?:\{[^}]*\}|%s|\%\(|\"\s*\+|\'\s*\+|\.format\()"
                r"|(?:execute|executemany)\s*\(\s*(?:f[\"\']|[^)]*(?:\+|%|\.format\()) ",
                "critical",
                "Possible SQL injection",
                "The query is assembled with string formatting instead of bound parameters.",
                "Use parameterised queries: cursor.execute(sql, params).",
            ),
            (
                r"subprocess\.(?:run|call|Popen|check_output)\([^)]*shell\s*=\s*True",
                "high",
                "Shell injection risk",
                "shell=True passes the command through a shell, so metacharacters are interpreted.",
                "Pass an argument list and leave shell=False.",
            ),
            (
                r"(?:password|passwd|secret|api_key|apikey|token)\s*=\s*[\"'][^\"']{6,}[\"']",
                "high",
                "Hardcoded credential",
                "Secrets in source leak through version control and images.",
                "Load it from the environment or a secret manager.",
            ),
            (
                r"\bpickle\.loads?\s*\(",
                "high",
                "Unsafe deserialisation",
                "pickle executes arbitrary code during load.",
                "Use JSON, or verify a signature before unpickling.",
            ),
            (
                r"\bmd5\s*\(|\bsha1\s*\(",
                "medium",
                "Weak hash function",
                "MD5/SHA-1 are collision-prone and unsuitable for security use.",
                "Use SHA-256, or bcrypt/argon2 for passwords.",
            ),
            (
                r"verify\s*=\s*False",
                "high",
                "TLS verification disabled",
                "Disabling certificate verification allows machine-in-the-middle attacks.",
                "Leave verification on and install the proper CA bundle.",
            ),
            (
                r"\brandom\.(?:random|randint|choice)\s*\(",
                "medium",
                "Non-cryptographic randomness",
                "random is seeded predictably and must not generate secrets.",
                "Use the secrets module for tokens and keys.",
            ),
        ]
        issues = []
        for i, line in enumerate(code.splitlines(), start=1):
            for rx, sev, title, why, fix in patterns:
                if re.search(rx, line, re.IGNORECASE):
                    issues.append(
                        {
                            "severity": sev,
                            "category": "security",
                            "line": i,
                            "title": title,
                            "explanation": why,
                            "suggestion": fix,
                        }
                    )
        return json.dumps({"issues": issues[:15]})

    def _task_code_summary(self, prompt: str, system: str | None) -> str:
        try:
            findings = extract_json(self._section(prompt, "FINDINGS") or prompt)
        except LLMError:
            findings = []
        if isinstance(findings, dict):
            findings = findings.get("issues", [])
        sev = [str(f.get("severity", "low")).lower() for f in findings if isinstance(f, dict)]
        if any(s in {"critical", "high"} for s in sev):
            verdict, summary = "request_changes", "Blocking issues found; changes required before merge."
        elif sev:
            verdict, summary = "comment", "No blockers, but several improvements are worth making."
        else:
            verdict, summary = "approve", "No issues detected in the reviewed snippet."
        counts = {s: sev.count(s) for s in set(sev)}
        return json.dumps(
            {
                "verdict": verdict,
                "summary": summary,
                "severity_counts": counts,
                "top_recommendation": (
                    findings[0].get("suggestion") if findings and isinstance(findings[0], dict) else None
                ),
            }
        )

    def _task_ticket_classification(self, prompt: str, system: str | None) -> str:
        text = (self._section(prompt, "TICKET") or prompt).lower()
        rules = [
            (("refund", "charge", "invoice", "billing", "payment", "card", "subscription"), "billing"),
            (("crash", "error", "500", "broken", "bug", "exception", "fail"), "bug"),
            (("password", "login", "sign in", "locked", "2fa", "access"), "account"),
            (("how do i", "how to", "documentation", "tutorial", "guide"), "how_to"),
            (("feature", "request", "would be nice", "suggestion", "roadmap"), "feature_request"),
        ]
        category = "general"
        for keys, cat in rules:
            if any(k in text for k in keys):
                category = cat
                break
        if any(
            k in text for k in ("urgent", "outage", "down", "data loss", "asap", "critical", "production")
        ):
            priority = "urgent"
        elif any(k in text for k in ("cannot", "can't", "blocked", "failing", "charged twice")):
            priority = "high"
        elif any(k in text for k in ("slow", "confusing", "minor", "typo")):
            priority = "low"
        else:
            priority = "medium"
        return json.dumps(
            {
                "priority": priority,
                "category": category,
                "confidence": 0.72,
                "reason": "Keyword-rule classification from the LLM fallback path.",
            }
        )

    def _task_support_draft(self, prompt: str, system: str | None) -> str:
        kb = self._section(prompt, "KNOWLEDGE BASE")
        ticket = self._section(prompt, "TICKET")
        sentences = split_sentences(kb)

        # Two sentences, kept in source order. Support guidance is usually
        # "here is what happened" followed by "here is what we will do", and a
        # reply carrying only the first half is not actually useful.
        #
        # Scored by overlap *weighted by informativeness*: a section heading
        # like "Duplicate charges." overlaps a duplicate-charge ticket almost
        # perfectly while telling the customer nothing, so short fragments are
        # discounted in favour of sentences that actually carry an answer.
        def informativeness(sentence: str) -> float:
            return min(1.0, len(content_words(sentence)) / 6.0)

        ranked = sorted(
            range(len(sentences)),
            key=lambda i: lexical_overlap(ticket, sentences[i]) * informativeness(sentences[i]),
            reverse=True,
        )[:2]
        body = " ".join(sentences[i] for i in sorted(ranked))
        body = body or "A support specialist will follow up with the details shortly."
        return (
            "Hi there,\n\nThanks for reaching out. "
            f"{body}\n\nIf that does not resolve it, reply here and we will pick it straight back up.\n\n"
            "Best regards,\nHelix Support"
        )

    def _task_escalation(self, prompt: str, system: str | None) -> str:
        ticket = (self._section(prompt, "TICKET") or prompt).lower()
        priority = (self._section(prompt, "PRIORITY") or "").strip().lower()
        triggers = [
            k
            for k in ("outage", "data loss", "security", "breach", "legal", "churn", "cancel")
            if k in ticket
        ]
        escalate = priority == "urgent" or bool(triggers)
        return json.dumps(
            {
                "escalate": escalate,
                "reason": (
                    f"Priority is {priority or 'unset'}"
                    + (f" and the ticket mentions {', '.join(triggers)}" if triggers else "")
                    + "; routing to a human owner."
                )
                if escalate
                else "Standard-severity request; the drafted reply can be sent by the support queue.",
                "suggested_owner": "on-call-engineer" if escalate else "support-queue",
            }
        )

    def _task_judge(self, prompt: str, system: str | None) -> str:
        """LLM-as-judge used by the Phase 8 quality gate."""
        metric = (self._section(prompt, "METRIC") or "").strip().lower()
        answer = self._section(prompt, "ANSWER")
        context = self._section(prompt, "CONTEXT")
        question = self._section(prompt, "QUESTION")
        if metric == "faithfulness":
            score = coverage(answer, context)
        elif metric == "answer_relevance":
            score = min(1.0, lexical_overlap(question, answer) * 3.0)
        elif metric == "context_precision":
            ground_truth = self._section(prompt, "GROUND TRUTH") or question
            score = min(1.0, coverage(ground_truth, context))
        else:
            score = lexical_overlap(question, answer)
        return json.dumps({"score": round(min(1.0, max(0.0, score)), 3), "metric": metric})

    def _task_tool_router(self, prompt: str, system: str | None) -> str:
        question = (self._section(prompt, "QUESTION") or prompt).lower()
        m = re.search(r"\b(?:order|ord)[\s#-]*([a-z0-9]{4,})\b", question)
        if m and any(k in question for k in ("order", "shipment", "delivery", "tracking", "status")):
            return json.dumps({"tool": "lookup_order_status", "arguments": {"order_id": m.group(1).upper()}})
        return json.dumps({"tool": None, "arguments": {}})


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


class BaseProvider:
    name = "base"

    async def complete(
        self, prompt: str, *, system: str | None, task: str, temperature: float, max_tokens: int
    ) -> LLMResponse:
        raise NotImplementedError

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        raise NotImplementedError


class MockProvider(BaseProvider):
    name = "mock"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.brain = MockBrain()
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, prompt: str, *, system: str | None, task: str, temperature: float, max_tokens: int
    ) -> LLMResponse:
        text = self.brain.respond(task, prompt, system)
        self.calls.append({"task": task, "prompt": prompt})
        return LLMResponse(
            text=text,
            model=f"mock-{task}",
            provider="mock",
            prompt_tokens=estimate_tokens(prompt + (system or "")),
            completion_tokens=estimate_tokens(text),
        )

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        dim = self.settings.embedding_dim
        return [hashed_embedding(t, dim) for t in texts], sum(estimate_tokens(t) for t in texts)


class OpenAIProvider(BaseProvider):
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        from openai import AsyncOpenAI  # imported lazily: never needed in mock mode

        if not settings.openai_api_key:
            raise LLMError("LLM_PROVIDER=openai requires OPENAI_API_KEY")
        self.settings = settings
        self.client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=settings.llm_timeout_seconds)

    async def complete(
        self, prompt: str, *, system: str | None, task: str, temperature: float, max_tokens: int
    ) -> LLMResponse:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            resp = await self.client.chat.completions.create(
                model=self.settings.openai_model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            raise LLMError(f"OpenAI call failed: {exc}") from exc
        usage = resp.usage
        return LLMResponse(
            text=resp.choices[0].message.content or "",
            model=resp.model,
            provider="openai",
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            raw=resp,
        )

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        resp = await self.client.embeddings.create(model=self.settings.embedding_model, input=texts)
        return [d.embedding for d in resp.data], getattr(resp.usage, "total_tokens", 0) or 0


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        from anthropic import AsyncAnthropic  # lazy import

        if not settings.anthropic_api_key:
            raise LLMError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
        self.settings = settings
        self.client = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=settings.llm_timeout_seconds)

    async def complete(
        self, prompt: str, *, system: str | None, task: str, temperature: float, max_tokens: int
    ) -> LLMResponse:
        try:
            resp = await self.client.messages.create(
                model=self.settings.llm_model,
                system=system or "You are a helpful assistant.",
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            raise LLMError(f"Anthropic call failed: {exc}") from exc
        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        return LLMResponse(
            text=text,
            model=resp.model,
            provider="anthropic",
            prompt_tokens=resp.usage.input_tokens,
            completion_tokens=resp.usage.output_tokens,
            raw=resp,
        )

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        # Anthropic ships no first-party embedding endpoint; fall back to the
        # deterministic local embedder so the RAG pod stays usable.
        dim = self.settings.embedding_dim
        return [hashed_embedding(t, dim) for t in texts], sum(estimate_tokens(t) for t in texts)


# --------------------------------------------------------------------------- #
# Public client
# --------------------------------------------------------------------------- #


class LLMClient:
    """The single interface every agent in Helix calls."""

    def __init__(self, settings: Settings | None = None, provider: BaseProvider | None = None) -> None:
        self.settings = settings or get_settings()
        self.provider = provider or self._build_provider(self.settings)

    @staticmethod
    def _build_provider(settings: Settings) -> BaseProvider:
        match settings.llm_provider:
            case "openai":
                return OpenAIProvider(settings)
            case "anthropic":
                return AnthropicProvider(settings)
            case _:
                return MockProvider(settings)

    @property
    def provider_name(self) -> str:
        return self.provider.name

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        task: str = "default",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        resp = await self.provider.complete(
            prompt,
            system=system,
            task=task,
            temperature=self.settings.llm_temperature if temperature is None else temperature,
            max_tokens=max_tokens or self.settings.llm_max_tokens,
        )
        resp.latency_ms = (time.perf_counter() - started) * 1000
        return resp

    async def complete_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        task: str = "default",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[Any, LLMResponse]:
        """Complete and parse. Returns (parsed, response) so callers keep usage data."""
        resp = await self.complete(
            prompt, system=system, task=task, temperature=temperature, max_tokens=max_tokens
        )
        return resp.json(), resp

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        if not texts:
            return [], 0
        if self.settings.embedding_provider == "openai" and self.settings.openai_api_key:
            provider: BaseProvider = (
                self.provider if isinstance(self.provider, OpenAIProvider) else OpenAIProvider(self.settings)
            )
            return await provider.embed(texts)
        dim = self.settings.embedding_dim
        return [hashed_embedding(t, dim) for t in texts], sum(estimate_tokens(t) for t in texts)

    async def embed_one(self, text: str) -> list[float]:
        vectors, _ = await self.embed([text])
        return vectors[0]


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def reset_llm_client() -> None:
    """Test hook: drop the cached singleton so settings changes take effect."""
    global _client
    _client = None
