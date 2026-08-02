#!/usr/bin/env python3
"""End-to-end smoke test against a running Helix deployment.

Exercises every pod plus auth, caching, RBAC and billing through the real HTTP
API. Standard library only, so it runs anywhere -- against docker compose,
staging, or production.

    docker compose up -d
    python scripts/smoke_test.py
    python scripts/smoke_test.py https://api.helix.example.com

Exits non-zero if any check fails, so it works as a deploy gate.

Note: the observability check needs an admin, and only the *first* account on an
instance becomes one. Against a database that already has users, that check
asserts the 403 instead.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")


def call(path, data=None, token=None, method=None):
    """Returns (status, json_body, lowercased_headers)."""
    request = urllib.request.Request(
        BASE + path, method=method or ("POST" if data is not None else "GET")
    )
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    body = json.dumps(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(request, body, timeout=60) as response:
            payload = response.read() or b"{}"
            # Lowercased: HTTP/1.1 sends header names lowercased on the wire.
            headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, json.loads(payload), headers
    except urllib.error.HTTPError as exc:
        payload = exc.read() or b"{}"
        headers = {k.lower(): v for k, v in exc.headers.items()}
        try:
            return exc.code, json.loads(payload), headers
        except json.JSONDecodeError:
            return exc.code, {}, headers


results: list[bool] = []


def check(label: str, passed: bool, detail: str = "") -> bool:
    print(f"{'PASS' if passed else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    results.append(passed)
    return passed


def main() -> int:
    print(f"Helix smoke test against {BASE}\n")

    status, body, _ = call("/health")
    check(
        "health",
        status == 200 and body.get("status") == "ok",
        f"db={body.get('database')} redis={body.get('redis')} llm={body.get('llm_provider')}",
    )

    email = f"smoke-{int(time.time())}@helix.example.com"
    status, body, _ = call("/auth/signup", {"email": email, "password": "smokepass123"})
    token = body.get("tokens", {}).get("access_token", "")
    role = body.get("user", {}).get("role")
    check("signup", status == 201 and bool(token), f"role={role}")
    if not token:
        return 1

    status, body, _ = call(
        "/docs/ingest",
        {
            "title": "Smoke Policy",
            "text": (
                "Trial period. Every new Helix workspace begins with a free trial that lasts "
                "14 days from the date of signup. Refunds. Customers may request a full refund "
                "within 30 days of their first payment."
            ),
        },
        token,
    )
    check(
        "ingest", status == 201 and body.get("chunk_count", 0) > 0, f"chunks={body.get('chunk_count')}"
    )

    question = {"question": "How long does the free trial last?"}
    status, body, headers = call("/docs/query", question, token)
    check(
        "grounded answer",
        status == 200 and "14 days" in body.get("answer", ""),
        f"cache={headers.get('x-cache')} llm_calls={body['usage']['llm_calls']}",
    )

    status, cached, headers = call("/docs/query", question, token)
    check(
        "semantic cache hit costs nothing",
        headers.get("x-cache") == "HIT" and cached["usage"]["cost_usd"] == 0.0,
        f"cache={headers.get('x-cache')} cost=${cached['usage']['cost_usd']}",
    )

    status, body, _ = call(
        "/docs/query",
        {"question": "What is the airspeed velocity of an unladen swallow?", "use_cache": False},
        token,
    )
    check("abstains on an out-of-scope question", body.get("found") is False)

    status, body, _ = call(
        "/code-review/analyze",
        {"code": 'import subprocess\ndef run(x):\n    subprocess.run("echo " + x, shell=True)\n'},
        token,
    )
    check(
        "code review flags the vulnerability",
        status == 201 and body.get("verdict") == "request_changes",
        f"issues={body.get('issue_count')}",
    )

    status, body, _ = call(
        "/support/triage",
        {
            "subject": "Charged twice",
            "body": "I was charged twice for my subscription, please refund the duplicate payment.",
        },
        token,
    )
    check(
        "support triage",
        status == 201 and body.get("category") == "billing",
        f"path={body.get('classification_path')} confidence={body.get('confidence')}",
    )

    status, body, _ = call("/billing/usage", token=token)
    check(
        "billing usage",
        status == 200 and body.get("tier") == "free",
        f"used={body.get('used')}/{body.get('limit')}",
    )

    status, body, _ = call("/observability/summary", token=token)
    if role == "admin":
        check(
            "observability summary",
            status == 200 and body.get("total_requests", 0) > 0,
            f"pods={[p['pod'] for p in body.get('pods', [])]} "
            f"spend=${body.get('total_cost_usd')} saved=${body.get('estimated_cost_saved_usd')}",
        )
    else:
        # Not the first account, so a 403 here is the RBAC guard doing its job.
        check("observability is admin-only", status == 403, "not the first account on this instance")

    passed = sum(1 for r in results if r)
    print(f"\n{'ALL PASSED' if all(results) else 'FAILURES'} ({passed}/{len(results)})")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
