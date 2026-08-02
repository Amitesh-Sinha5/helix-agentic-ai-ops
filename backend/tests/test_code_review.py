"""Phase 5: the Code Review pod."""

from __future__ import annotations

import httpx

VULNERABLE = """
import subprocess, pickle, hashlib

API_KEY = "sk_live_abcdef1234567890abcdef"

def run_report(user_input, results=[]):
    query = f"SELECT * FROM reports WHERE name = '{user_input}'"
    cursor.execute(query)
    subprocess.run("echo " + user_input, shell=True)
    data = pickle.loads(open("cache.bin", "rb").read())
    digest = hashlib.md5(user_input.encode()).hexdigest()
    try:
        results.append(eval(user_input))
    except:
        print("failed")
    return results
"""

CLEAN = '''
def add(a: int, b: int) -> int:
    """Return the sum of two integers."""
    return a + b
'''


async def test_analyze_returns_structured_findings_not_prose(user_client: httpx.AsyncClient):
    response = await user_client.post(
        "/code-review/analyze", json={"code": VULNERABLE, "language": "python", "filename": "reports.py"}
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["verdict"] == "request_changes"
    assert body["issue_count"] > 0
    assert body["blocking_count"] > 0

    for issue in body["issues"]:
        assert issue["severity"] in ("critical", "high", "medium", "low")
        assert issue["title"] and issue["explanation"]
        assert issue["agent"] in ("quality", "security")
        if issue["line"] is not None:
            assert issue["line"] >= 1

    assert body["review_id"]
    assert body["usage"]["llm_calls"] >= 3, "quality, security and summarizer should each run"


async def test_both_reviewer_agents_contribute(user_client: httpx.AsyncClient):
    body = (await user_client.post("/code-review/analyze", json={"code": VULNERABLE})).json()
    agents = {issue["agent"] for issue in body["issues"]}
    assert agents == {"quality", "security"}, f"expected both reviewers, got {agents}"


async def test_security_agent_finds_the_real_vulnerabilities(user_client: httpx.AsyncClient):
    body = (await user_client.post("/code-review/analyze", json={"code": VULNERABLE})).json()
    titles = " ".join(i["title"].lower() for i in body["issues"])

    for expected in ("sql injection", "shell injection", "hardcoded credential", "arbitrary code execution"):
        assert expected in titles, f"missed {expected}: {titles}"


async def test_quality_agent_finds_the_mutable_default(user_client: httpx.AsyncClient):
    body = (await user_client.post("/code-review/analyze", json={"code": VULNERABLE})).json()
    mutable = [i for i in body["issues"] if "mutable default" in i["title"].lower()]
    assert mutable, "mutable default argument was not reported"
    assert mutable[0]["severity"] == "high"
    assert mutable[0]["agent"] == "quality"


async def test_issues_are_ordered_by_severity(user_client: httpx.AsyncClient):
    body = (await user_client.post("/code-review/analyze", json={"code": VULNERABLE})).json()
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    severities = [rank[i["severity"]] for i in body["issues"]]
    assert severities == sorted(severities), "most severe findings must come first"


async def test_clean_code_is_approved(user_client: httpx.AsyncClient):
    body = (await user_client.post("/code-review/analyze", json={"code": CLEAN})).json()
    assert body["verdict"] == "approve"
    assert body["issues"] == []
    assert body["blocking_count"] == 0


async def test_verdict_is_forced_to_match_the_findings(user_client: httpx.AsyncClient):
    """The summarizer cannot approve code that has blocking issues."""
    body = (await user_client.post("/code-review/analyze", json={"code": VULNERABLE})).json()
    assert body["blocking_count"] > 0
    assert body["verdict"] == "request_changes"
    assert sum(body["severity_counts"].values()) == body["issue_count"]


async def test_findings_are_deduplicated(user_client: httpx.AsyncClient):
    body = (await user_client.post("/code-review/analyze", json={"code": VULNERABLE})).json()
    keys = [(i["line"], i["title"]) for i in body["issues"]]
    assert len(keys) == len(set(keys)), "the same finding was reported twice"


async def test_review_is_persisted_and_retrievable(user_client: httpx.AsyncClient):
    created = (
        await user_client.post("/code-review/analyze", json={"code": VULNERABLE, "filename": "a.py"})
    ).json()

    listing = (await user_client.get("/code-review/reviews")).json()
    assert listing["total"] == 1
    assert listing["items"][0]["filename"] == "a.py"

    detail = (await user_client.get(f"/code-review/reviews/{created['review_id']}")).json()
    assert detail["verdict"] == created["verdict"]
    assert len(detail["issues"]) == created["issue_count"]


async def test_another_user_cannot_read_the_review(client: httpx.AsyncClient, user_client):
    created = (await user_client.post("/code-review/analyze", json={"code": CLEAN})).json()
    other = await client.post(
        "/auth/signup", json={"email": "nosy@helix.example.com", "password": "passw0rd1"}
    )
    token = other.json()["tokens"]["access_token"]

    response = await client.get(
        f"/code-review/reviews/{created['review_id']}", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 404


async def test_analyze_requires_authentication(client: httpx.AsyncClient):
    assert (await client.post("/code-review/analyze", json={"code": "x = 1"})).status_code == 401


async def test_empty_code_is_rejected(user_client: httpx.AsyncClient):
    assert (await user_client.post("/code-review/analyze", json={"code": ""})).status_code == 422


async def test_trace_covers_the_parallel_agents(user_client: httpx.AsyncClient):
    body = (await user_client.post("/code-review/analyze", json={"code": VULNERABLE})).json()
    nodes = {e["node"] for e in body["trace"] if e["phase"] == "finish"}
    assert nodes == {"quality_agent", "security_agent", "summarizer"}
