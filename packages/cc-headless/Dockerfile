FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
RUN uv sync --frozen --no-dev

FROM node:22-slim AS node-dist

FROM python:3.12-slim

COPY --from=node-dist /usr/local/bin/node /usr/local/bin/
COPY --from=node-dist /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ARG GITHUB_MCP_VERSION=v1.0.2
RUN ARCH=$(dpkg --print-architecture | sed 's/amd64/x86_64/;s/arm64/arm64/') && \
    curl -fsSL "https://github.com/github/github-mcp-server/releases/download/${GITHUB_MCP_VERSION}/github-mcp-server_Linux_${ARCH}.tar.gz" | tar -xz -C /usr/local/bin github-mcp-server && \
    chmod +x /usr/local/bin/github-mcp-server

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY prompts/ ./prompts/
COPY .claude/ ./.claude/
COPY mcp-config.json ./
COPY CLAUDE.md ./

ENV PATH="/app/.venv/bin:$PATH"

RUN useradd -m -s /bin/bash ccuser \
    && chown -R ccuser:ccuser /app

USER ccuser
ENV HOME="/home/ccuser"
ENV UV_CACHE_DIR="/app/.uv-cache"
ENV UV_TOOL_DIR="/app/.uv-tools"
ENV UV_TOOL_BIN_DIR="/app/.uv-tools/bin"

RUN uv tool install fastmcp \
    && uv tool install "awslabs.cloudwatch-mcp-server" \
    && uv tool install "awslabs.cloudtrail-mcp-server"

ENV UV_OFFLINE=true

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"]

CMD ["python", "-m", "cc_headless.main"]
