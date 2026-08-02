"""Loads and serves the trained ticket classifier.

Artefact produced by `scripts/train_classifier.py`. Loaded once at startup and
reused, because a scikit-learn predict is sub-millisecond and there is no reason
to pay import cost per request.

The confidence returned is the *minimum* of the two heads' top probabilities,
not their average: triage needs both priority and category to be right, so the
weaker head should govern whether the LLM fallback fires.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger("helix.support.classifier")


@dataclass
class Prediction:
    priority: str
    category: str
    confidence: float
    priority_confidence: float = 0.0
    category_confidence: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    available: bool = True

    @property
    def is_confident(self) -> bool:
        return self.available and self.confidence >= get_settings().classifier_confidence_threshold


class TicketClassifier:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or get_settings().classifier_path)
        self._artefact: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._load_failed = False

    @property
    def available(self) -> bool:
        return self.load() is not None

    @property
    def metrics(self) -> dict[str, Any]:
        artefact = self.load()
        return (artefact or {}).get("metrics", {})

    def load(self) -> dict[str, Any] | None:
        if self._artefact is not None:
            return self._artefact
        if self._load_failed:
            return None
        with self._lock:
            if self._artefact is not None:
                return self._artefact
            path = self.path
            if not path.is_absolute():
                # Resolve relative to the backend package root so the app works
                # regardless of the process working directory.
                path = Path(__file__).resolve().parents[2] / path
            if not path.exists():
                logger.warning(
                    "Classifier artefact not found at %s -- every ticket will use the LLM "
                    "fallback path. Run: python -m scripts.train_classifier",
                    path,
                )
                self._load_failed = True
                return None
            try:
                import joblib

                self._artefact = joblib.load(path)
                logger.info(
                    "Loaded ticket classifier from %s (held-out accuracy: %s)",
                    path,
                    {k: v.get("accuracy") for k, v in self._artefact.get("metrics", {}).items()},
                )
            except Exception:
                logger.exception("Failed to load classifier from %s", path)
                self._load_failed = True
                return None
        return self._artefact

    def predict(self, text: str) -> Prediction:
        artefact = self.load()
        if artefact is None or not text.strip():
            return Prediction(priority="medium", category="general", confidence=0.0, available=False)

        priority, p_conf, p_probs = self._predict_one(artefact["priority_model"], text)
        category, c_conf, c_probs = self._predict_one(artefact["category_model"], text)

        return Prediction(
            priority=priority,
            category=category,
            # Both heads must be right for triage to be right, so the weaker one
            # decides whether we trust this without an LLM.
            confidence=round(min(p_conf, c_conf), 4),
            priority_confidence=round(p_conf, 4),
            category_confidence=round(c_conf, 4),
            probabilities={
                **{f"priority.{k}": round(v, 4) for k, v in p_probs.items()},
                **{f"category.{k}": round(v, 4) for k, v in c_probs.items()},
            },
        )

    @staticmethod
    def _predict_one(model: Any, text: str) -> tuple[str, float, dict[str, float]]:
        probabilities = model.predict_proba([text])[0]
        classes = list(model.classes_)
        best = int(max(range(len(probabilities)), key=lambda i: probabilities[i]))
        return (
            str(classes[best]),
            float(probabilities[best]),
            {str(c): float(p) for c, p in zip(classes, probabilities, strict=True)},
        )


_classifier: TicketClassifier | None = None


def get_classifier() -> TicketClassifier:
    global _classifier
    if _classifier is None:
        _classifier = TicketClassifier()
    return _classifier


def reset_classifier() -> None:
    global _classifier
    _classifier = None
