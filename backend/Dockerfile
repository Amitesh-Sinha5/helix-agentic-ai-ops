# syntax=docker/dockerfile:1

# --- builder ---------------------------------------------------------------
# Dependencies are built into a virtualenv here and copied into a clean runtime
# image, so no compiler toolchain ships to production.
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copied alone so the (slow) dependency layer is cached until requirements change.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# --- runtime ---------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 helix

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=helix:helix alembic.ini pytest.ini ./
COPY --chown=helix:helix alembic ./alembic
COPY --chown=helix:helix app ./app
COPY --chown=helix:helix scripts ./scripts
COPY --chown=helix:helix tests ./tests
COPY --chown=helix:helix docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# The classifier artefact is a build product, not source. Training it here means
# the image is self-contained and the first triage request is not slow.
RUN python -m scripts.train_classifier --quiet \
    && chown helix:helix app/support/classifier.pkl

# Writable state: Chroma's index lives here.
RUN mkdir -p /app/.chroma && chown -R helix:helix /app/.chroma
VOLUME ["/app/.chroma"]

USER helix
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
