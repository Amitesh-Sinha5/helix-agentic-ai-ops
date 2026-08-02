"""Guardrails: structured-output validation, PII redaction, and input screening.

Two directions of defence:

* **Outbound** -- `validate_output` forces every agent's free-form model text
  into a declared Pydantic schema, with one bounded repair attempt before it
  gives up. Nothing leaves an agent as unvalidated text.
* **Inbound/outbound PII** -- `PIIRedactor` strips emails, phone numbers, card
  numbers, SSNs, IPs, API keys and JWTs from anything about to be logged,
  embedded, or sent to a third-party model.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.llm_client import LLMError, extract_json

logger = logging.getLogger("helix.guardrails")

T = TypeVar("T", bound=BaseModel)


class GuardrailViolation(ValueError):
    """Raised when output cannot be coerced into the required schema."""

    def __init__(self, message: str, *, errors: Any = None, raw: str | None = None) -> None:
        super().__init__(message)
        self.errors = errors
        self.raw = raw


# --------------------------------------------------------------------------- #
# PII redaction
# --------------------------------------------------------------------------- #


@dataclass
class RedactionResult:
    text: str
    findings: dict[str, int] = field(default_factory=dict)

    @property
    def redacted(self) -> bool:
        return bool(self.findings)


class PIIRedactor:
    """Regex-based PII redaction.

    Ordering matters: the more specific patterns (card, SSN) run before the
    looser numeric ones so a card number is never mislabelled as a phone number.
    """

    PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")),
        ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")),
        ("API_KEY", re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj)?[-_]?[A-Za-z0-9]{16,}\b")),
        ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
        ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
        ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
        ("PHONE", re.compile(r"(?<!\w)(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?!\w)")),
        ("IP_ADDRESS", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    )

    def __init__(self, enabled_types: set[str] | None = None) -> None:
        self.enabled = enabled_types

    @staticmethod
    def _luhn(digits: str) -> bool:
        """Only redact card-shaped numbers that actually pass Luhn, so version
        strings and long IDs are not mangled."""
        nums = [int(c) for c in digits if c.isdigit()]
        if not 13 <= len(nums) <= 19:
            return False
        checksum, parity = 0, len(nums) % 2
        for i, n in enumerate(nums):
            if i % 2 == parity:
                n *= 2
                if n > 9:
                    n -= 9
            checksum += n
        return checksum % 10 == 0

    def redact(self, text: str) -> RedactionResult:
        if not text:
            return RedactionResult(text=text)
        findings: dict[str, int] = {}
        out = text
        for label, pattern in self.PATTERNS:
            if self.enabled is not None and label not in self.enabled:
                continue

            def _sub(match: re.Match[str], _label: str = label) -> str:
                value = match.group(0)
                if _label == "CREDIT_CARD" and not self._luhn(value):
                    return value
                if _label == "IP_ADDRESS" and any(int(o) > 255 for o in value.split(".")):
                    return value
                findings[_label] = findings.get(_label, 0) + 1
                return f"[REDACTED_{_label}]"

            out = pattern.sub(_sub, out)
        return RedactionResult(text=out, findings=findings)

    def scrub(self, value: Any) -> Any:
        """Recursively redact strings inside dicts/lists (for log payloads)."""
        if isinstance(value, str):
            return self.redact(value).text
        if isinstance(value, dict):
            return {k: self.scrub(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.scrub(v) for v in value)
        return value


_redactor = PIIRedactor()


def redact_pii(text: str) -> str:
    return _redactor.redact(text).text


def scan_pii(text: str) -> dict[str, int]:
    return _redactor.redact(text).findings


# --------------------------------------------------------------------------- #
# Prompt-injection screening
# --------------------------------------------------------------------------- #

INJECTION_PATTERNS = (
    re.compile(r"ignore (?:all |any )?(?:the )?(?:previous|prior|above) instructions", re.I),
    re.compile(r"disregard (?:the )?(?:system|previous) prompt", re.I),
    re.compile(r"reveal (?:your )?(?:system prompt|instructions)", re.I),
    re.compile(r"you are now (?:a|an|in) \w+", re.I),
    re.compile(r"<\|im_(?:start|end)\|>", re.I),
)


def screen_input(text: str) -> list[str]:
    """Return the names of any prompt-injection patterns present in user input.

    Retrieved document text is untrusted too -- ingested content is screened on
    the same path before it can reach a model as context.
    """
    return [p.pattern for p in INJECTION_PATTERNS if p.search(text)]


# --------------------------------------------------------------------------- #
# Structured-output validation
# --------------------------------------------------------------------------- #


def validate_output(model: type[T], payload: Any, *, raw: str | None = None) -> T:
    """Coerce a parsed payload into `model` or raise GuardrailViolation."""
    if isinstance(payload, model):
        return payload
    if isinstance(payload, str):
        try:
            payload = extract_json(payload)
        except LLMError as exc:
            raise GuardrailViolation(str(exc), raw=raw or payload) from exc
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise GuardrailViolation(
            f"Output failed {model.__name__} validation", errors=exc.errors(), raw=raw
        ) from exc


async def validated_completion(
    llm: Any,
    model: type[T],
    prompt: str,
    *,
    operation: str,
    system: str | None = None,
    task: str | None = None,
    repair: bool = True,
    **kwargs: Any,
) -> T:
    """Call an LLM and return a validated object, with one repair attempt.

    Works with either `LLMClient` or `TracedLLM`; the latter takes an
    `operation` kwarg, which we pass through when it is supported.
    """
    call_kwargs = dict(kwargs)
    call_kwargs["system"] = system
    call_kwargs["task"] = task or operation
    supports_operation = "operation" in getattr(type(llm).complete, "__annotations__", {}) or hasattr(
        llm, "telemetry"
    )
    if supports_operation:
        call_kwargs["operation"] = operation

    response = await llm.complete(prompt, **call_kwargs)
    try:
        return validate_output(model, response.text, raw=response.text)
    except GuardrailViolation as first_error:
        if not repair:
            raise
        logger.info("guardrail repair triggered for %s: %s", operation, first_error)
        repair_prompt = (
            f"{prompt}\n\nPREVIOUS RESPONSE:\n{response.text}\n\n"
            f"ERROR:\nThe previous response did not match the required schema "
            f"({first_error.errors}). Reply with ONLY valid JSON matching "
            f"{model.model_json_schema()}."
        )
        if supports_operation:
            call_kwargs["operation"] = f"{operation}.repair"
        repaired = await llm.complete(repair_prompt, **call_kwargs)
        return validate_output(model, repaired.text, raw=repaired.text)


class SafeText(BaseModel):
    """Convenience wrapper for free-text agent output that must still be clean."""

    text: str

    @classmethod
    def from_model_output(cls, text: str) -> SafeText:
        return cls(text=redact_pii(text.strip()))
