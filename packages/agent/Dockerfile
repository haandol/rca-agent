FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
RUN uv sync --frozen --no-dev

FROM python:3.12-slim

RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --no-create-home app

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

USER app

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"]

CMD ["python", "-m", "rca_agent.main"]
