# Running Helix

Two ways to run it. **Option B is faster for development**; Option A is closer to production.

Neither needs an API key — the default LLM provider is a deterministic mock that runs entirely offline.

---

## Prerequisites

| | Required | Notes |
|---|---|---|
| **Python 3.11** | yes | **Not 3.12+.** Chroma, torch and scikit-learn wheels lag behind. |
| **Node 20.19+** | yes | 20.17 works but Vite prints a warning on every build. |
| **Docker** | only for Option A | On this Mac that means Colima — see below. |

> ### ⚠️ The one thing that will bite you
>
> Your default `python3` is **3.14**, which cannot install this project's
> dependencies. Always use `python3.11` explicitly:
>
> ```bash
> python3 --version     # Python 3.14.3  ← do not use
> python3.11 --version  # Python 3.11.15 ← use this
> ```
>
> The virtualenv in `backend/.venv` is already built with 3.11, so as long as you
> call `.venv/bin/python` you are fine.

---

## Option A — Docker (the whole stack)

Runs the API, the web app, Postgres and Redis together. One command.

```bash
# Start the container runtime first (Colima, not Docker Desktop)
colima start --cpu 4 --memory 6

# Bring everything up
docker compose up --build
```

First build takes ~3–5 minutes; afterwards it is seconds. Then:

| | |
|---|---|
| Web app | http://localhost:5173 |
| API | http://localhost:8000 |
| Interactive API docs | http://localhost:8000/api-docs |

Verify it actually works:

```bash
python3 scripts/smoke_test.py     # expect ALL PASSED (10/10)
```

Stop it:

```bash
docker compose down       # keep data
docker compose down -v    # wipe the database and index too
```

---

## Option B — Local development (recommended while coding)

Hot reload on both sides, no containers. SQLite stands in for Postgres and an
in-process fake stands in for Redis, so neither needs installing.

**Terminal 1 — backend:**

```bash
cd backend

# One-time setup (the venv already exists; skip if .venv/ is present)
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Every fresh checkout / after a model change
.venv/bin/alembic upgrade head          # create the tables
.venv/bin/python -m scripts.train_classifier   # train the triage classifier

# Run it
.venv/bin/uvicorn app.main:app --reload
```

**Terminal 2 — frontend:**

```bash
cd frontend
npm install        # one-time
npm run dev
```

Open http://localhost:5173.

> `scripts/train_classifier.py` writes `app/support/classifier.pkl`, which is
> gitignored because it is a build product. Without it, every ticket falls back
> to the LLM classifier — the app still works, it just costs more. The training
> run prints its held-out accuracy and fails if the model regresses.

---

## First run — a 3-minute walkthrough

**1. Create your account.** Go to http://localhost:5173, click *Create one*, sign
up with anything (`you@example.com` / `password123`).

> **The first account on a fresh database becomes the administrator.** Do this
> before anyone else does. Admins get the Observability page and the live
> escalation feed.

**2. Doc Q&A** — click **Load sample policy**, then ask:

> *How long does the free trial last?*

Watch the agent trace build in real time — retriever, sufficiency check, answer,
groundedness validator. Click any step to expand its structured detail (rerank
scores, chunk counts). You should get **"14 days"** with a citation.

**3. See the cache work** — ask the *same question again*. Note the `cache hit`
badge, near-zero latency, and `$0.00000` cost in the footer.

**4. See it refuse** — ask something the document cannot answer:

> *What is the recommended nitrogen mix for scuba diving below 40 metres?*

It returns "I could not find that in the provided documents" with no citations,
rather than making something up.

**5. Code Review** — the page is pre-filled with deliberately awful code. Click
**Review code**: a quality reviewer and a security reviewer run in parallel and
a summarizer merges their findings into one verdict.

> To review *your own* code, hit **Clear** first (or **Paste from clipboard**,
> or upload a file) — otherwise a plain paste lands inside the sample. The
> language is inferred from the filename extension.

**6. Support Triage** — try each of the three sample buttons:
- *Billing* → handled by the **trained model**, no LLM call
- *Outage* → escalates; watch the alert appear in the admin panel bottom-right
- *Ambiguous* → falls through to the **LLM fallback**

**7. Billing** — click **Upgrade to Pro**. With no Stripe keys configured this
runs a simulated checkout, and your rate limit lifts immediately.

**8. Observability** (admin only) — cost, latency, cache savings and measured
answer quality across all three pods.

---

## Tests

```bash
cd backend  && .venv/bin/python -m pytest          # 151 tests, ~38s
cd backend  && .venv/bin/python -m pytest -k quality_gate -s   # RAG quality scores
cd frontend && npm test                             # 43 tests
cd frontend && npx playwright test                  # 5 E2E (starts its own servers on :8100/:4174)
```

The load test needs a backend with rate limiting off:

```bash
cd backend
LLM_PROVIDER=mock RATE_LIMIT_ENABLED=false \
  DATABASE_URL="sqlite+aiosqlite:///./loadtest.db" \
  .venv/bin/python -m uvicorn app.main:app --port 8010 &
BASE_URL=http://127.0.0.1:8010 k6 run load-test/k6-script.js
```

---

## Using a real model

```bash
cd backend
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-... .venv/bin/uvicorn app.main:app --reload
```

Or copy `backend/.env.example` to `backend/.env` and edit it.

**Set the cost constants to your model's real prices**, or the observability
dashboard reports fiction:

```bash
COST_PER_1K_PROMPT_TOKENS=0.003
COST_PER_1K_COMPLETION_TOKENS=0.015
```

Expect **seconds**, not milliseconds, per uncached question — the Doc Q&A graph
makes 5–7 model calls. That is what the live trace is for.

### Running for free with a local model

`OPENAI_BASE_URL` points the OpenAI provider at any compatible server, so no
API key is needed:

```bash
ollama serve &
ollama pull qwen2.5:7b-instruct

cd backend
LLM_PROVIDER=openai \
  OPENAI_BASE_URL=http://localhost:11434/v1 \
  OPENAI_MODEL=qwen2.5:7b-instruct \
  .venv/bin/uvicorn app.main:app --reload
```

The same variable works for Groq, OpenRouter, Together, LM Studio and vLLM —
swap the URL and pass that service's key as `OPENAI_API_KEY`.

**Size the model to your VRAM.** On a 16 GB Mac (~11.8 GB usable) a 15 GB model
does not fit: it swaps to SSD and drops to *0.01 tokens/sec*, roughly 20 minutes
per question. A 3B model measured **27 tok/s** on the same machine — about 4
seconds per question. Check with `ollama ps` that `PROCESSOR` says `100% GPU`.

**Prefer 7B+ for this pipeline.** Every guardrailed node needs schema-valid
JSON. A 3B model manages the simple ones but fumbles others; optional nodes now
degrade gracefully rather than failing the request, but answer quality still
suffers. `qwen2.5:7b-instruct` is a good balance that fits comfortably.

---

## Make targets

```bash
make help        # list everything
make install     # set up both sides from scratch
make test        # every test suite
make lint        # lint + type-check both sides
make quality     # RAG quality gate, with scores printed
make up / down   # docker compose up --build -d / down -v
make smoke       # smoke-test a running deployment
make clean       # remove build and test artefacts
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No matching distribution found` on install | You used `python3` (3.14). Use `python3.11`. |
| `cannot connect to the Docker daemon` | `colima start --cpu 4 --memory 6` |
| `docker-credential-desktop: executable file not found` | Leftover from a Docker Desktop install. Remove `credsStore` from `~/.docker/config.json`. |
| `no such table: users` | Migrations never ran: `.venv/bin/alembic upgrade head` |
| Every ticket says `llm_fallback` | Classifier not trained: `.venv/bin/python -m scripts.train_classifier` |
| Frontend loads, all API calls fail | Backend not running, or on a different port than `VITE_API_URL` |
| Agent trace never appears | Backend restarted mid-request; the final answer still arrives (the response carries the full trace) |
| Port 5173 or 8000 already in use | `lsof -ti:8000 \| xargs kill -9` — a stale backend on :8000 also makes Playwright reuse it with the wrong CORS origin, which shows up in the browser as *Failed to fetch*. |
| `429 Rate limit exceeded` | Free tier is 20 requests/day. Upgrade on the Billing page, or set `RATE_LIMIT_ENABLED=false`. |
| Can't paste into the Code Review box | Use **Clear** first, or the **Paste from clipboard** button — the editor starts pre-filled with sample code. Plain ⌘V / Ctrl+V works too. |
| Want a clean slate | `docker compose down -v`, or delete `backend/helix.db` and `backend/.chroma/` |

---

## Where things live

```
backend/app/rag/          Doc Q&A: hybrid retrieval, agentic re-query loop
backend/app/code_review/  parallel quality + security reviewers
backend/app/support/      trained classifier → LLM fallback → draft → escalate
backend/app/core/         LLM client, telemetry, guardrails, cache, auth
frontend/src/pages/       one file per screen
```

Deploying to the internet: **[DEPLOYMENT.md](DEPLOYMENT.md)**.
What it does and why: **[README.md](README.md)**.
