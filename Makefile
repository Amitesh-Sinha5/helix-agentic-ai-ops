# Helix — common tasks.
.PHONY: help install test lint up down smoke clean

BACKEND := backend
FRONTEND := frontend
PY := $(BACKEND)/.venv/bin

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Set up backend venv and frontend deps
	cd $(BACKEND) && python3.11 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt
	cd $(BACKEND) && .venv/bin/python -m scripts.train_classifier --quiet
	cd $(FRONTEND) && npm ci

test: ## Run every test suite
	cd $(BACKEND) && .venv/bin/python -m pytest
	cd $(FRONTEND) && npm test
	cd $(FRONTEND) && npx playwright test

lint: ## Lint and type-check both sides
	cd $(BACKEND) && .venv/bin/ruff check app scripts tests && .venv/bin/ruff format --check app scripts tests
	cd $(FRONTEND) && npm run lint && npm run typecheck

quality: ## Run the RAG quality gate with scores
	cd $(BACKEND) && .venv/bin/python -m pytest -k quality_gate -s

up: ## Start the full stack in Docker
	docker compose up --build -d

down: ## Stop the stack and drop volumes
	docker compose down -v

smoke: ## Smoke-test a running deployment
	python3 scripts/smoke_test.py

load: ## Run the k6 load test (needs a backend on :8000 with rate limiting off)
	k6 run load-test/k6-script.js

clean: ## Remove build and test artefacts
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache $(BACKEND)/.chroma* $(BACKEND)/*.db
	rm -rf $(FRONTEND)/dist $(FRONTEND)/test-results $(FRONTEND)/playwright-report
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
