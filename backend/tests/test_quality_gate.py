"""Phase 8: the CI quality gate.

This is not a pass/fail string match. Every Doc Q&A case in the golden dataset is
scored on faithfulness, answer relevance and context precision, and the build
fails if the average of any metric drops below its configured floor. Break
retrieval, the reranker, the groundedness validator or the answer prompt and
these numbers move -- which is the entire point.

Run just this gate with:  pytest -k quality_gate
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.observability.evaluation import (
    EvaluationCase,
    RagEvaluator,
    aggregate,
    applicable_counts,
    check_thresholds,
    ragas_available,
    thresholds,
)

pytestmark = pytest.mark.quality_gate

GOLDEN_PATH = Path(__file__).parent / "golden_dataset.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text())


@pytest.fixture
async def loaded_corpus(user_client: httpx.AsyncClient, golden: dict) -> httpx.AsyncClient:
    for document in golden["corpus"]:
        response = await user_client.post(
            "/docs/ingest",
            json={
                "title": document["title"],
                "text": document["text"],
                "source": document["source"],
                "collection": document["collection"],
            },
        )
        assert response.status_code == 201, response.text
    return user_client


def test_golden_dataset_is_well_formed(golden: dict):
    total = len(golden["doc_qa"]) + len(golden["code_review"]) + len(golden["support_triage"])
    assert 10 <= total <= 30, f"golden set should hold 10-30 examples, has {total}"

    ids = [case["id"] for group in ("doc_qa", "code_review", "support_triage") for case in golden[group]]
    assert len(ids) == len(set(ids)), "duplicate case ids"

    # The set must contain genuine negatives, or "always answer" would pass it.
    assert any(not case["expected_found"] for case in golden["doc_qa"])


async def test_doc_qa_quality_gate(loaded_corpus: httpx.AsyncClient, golden: dict):
    """Score every Doc Q&A case and enforce the metric floors."""
    client = loaded_corpus
    cases: list[EvaluationCase] = []
    failures: list[str] = []

    for case in golden["doc_qa"]:
        response = await client.post("/docs/query", json={"question": case["question"], "use_cache": False})
        assert response.status_code == 200, response.text
        body = response.json()

        if body["found"] is not case["expected_found"]:
            failures.append(
                f"{case['id']}: expected found={case['expected_found']}, got {body['found']} "
                f"for {case['question']!r}"
            )
        for needle in case["must_contain"]:
            if needle.lower() not in body["answer"].lower():
                failures.append(f"{case['id']}: answer is missing {needle!r} -- got {body['answer'][:120]!r}")

        cases.append(
            EvaluationCase(
                case_id=case["id"],
                question=case["question"],
                answer=body["answer"],
                contexts=[chunk["text"] for chunk in body["chunks"]] if body["found"] else [],
                ground_truth=case["ground_truth"],
            )
        )

    assert not failures, "Doc Q&A correctness regressions:\n  " + "\n  ".join(failures)

    scores = await RagEvaluator().score_all(cases)
    averages = aggregate(scores)

    counts = applicable_counts(scores)
    print(f"\nRAG quality ({'ragas' if ragas_available() else 'LLM-as-judge'}), {len(scores)} cases:")
    for metric, value in averages.items():
        print(f"  {metric:<18} {value:.3f}  (floor {thresholds()[metric]:.2f}, n={counts[metric]})")
    worst = sorted(scores, key=lambda s: s.worst()[1])[:3]
    for score in worst:
        metric, value = score.worst()
        print(f"  weakest: {score.case_id} {metric}={value:.3f}")

    breaches = check_thresholds(averages)
    assert not breaches, "RAG quality gate failed:\n  " + "\n  ".join(breaches)


async def test_quality_gate_catches_a_broken_retriever(loaded_corpus: httpx.AsyncClient, golden: dict):
    """The gate must actually be able to fail.

    A gate that cannot go red is decoration, so this deliberately answers from
    unrelated context and asserts that faithfulness collapses below the floor.
    """
    sabotaged = [
        EvaluationCase(
            case_id=case["id"],
            question=case["question"],
            answer=case["ground_truth"],
            contexts=["Kubernetes autoscaling is configured with a horizontal pod autoscaler."],
            ground_truth=case["ground_truth"],
        )
        for case in golden["doc_qa"]
        if case["expected_found"]
    ]

    averages = aggregate(await RagEvaluator().score_all(sabotaged))
    assert check_thresholds(averages), f"the gate passed answers grounded in unrelated context: {averages}"


async def test_code_review_quality_gate(user_client: httpx.AsyncClient, golden: dict):
    failures: list[str] = []
    for case in golden["code_review"]:
        body = (await user_client.post("/code-review/analyze", json={"code": case["code"]})).json()

        if body["verdict"] != case["expected_verdict"]:
            failures.append(f"{case['id']}: expected {case['expected_verdict']}, got {body['verdict']}")
        titles = " ".join(issue["title"].lower() for issue in body["issues"])
        for needle in case["must_flag"]:
            if needle not in titles:
                failures.append(f"{case['id']}: did not flag {needle!r}")

    assert not failures, "Code review regressions:\n  " + "\n  ".join(failures)


async def test_support_triage_quality_gate(loaded_corpus: httpx.AsyncClient, golden: dict):
    failures: list[str] = []
    for case in golden["support_triage"]:
        body = (
            await loaded_corpus.post(
                "/support/triage", json={"subject": case["subject"], "body": case["body"]}
            )
        ).json()

        if body["category"] != case["expected_category"]:
            failures.append(
                f"{case['id']}: expected category {case['expected_category']}, got {body['category']}"
            )
        if body["escalate"] is not case["expected_escalate"]:
            failures.append(
                f"{case['id']}: expected escalate={case['expected_escalate']}, got {body['escalate']}"
            )
        if not body["draft_response"].strip():
            failures.append(f"{case['id']}: empty draft response")

    assert not failures, "Support triage regressions:\n  " + "\n  ".join(failures)


async def test_abstention_is_scored_as_correct_not_as_a_quality_failure():
    """Refusing to answer is the right behaviour, and must not be penalised."""
    scores = await RagEvaluator().score(
        EvaluationCase(
            case_id="abstain",
            question="Anything at all?",
            answer="I could not find that in the provided documents.",
            contexts=[],
        )
    )
    assert scores.faithfulness == 1.0
    assert scores.answer_relevance == 1.0
    # Undefined, not zero: there was no retrieved context to be precise about.
    assert scores.context_precision is None
    assert scores.notes["abstained"] is True
    assert aggregate([scores])["context_precision"] == 0.0  # no applicable cases
