FROM node:22-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends git python3 python3-pip pipx && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

RUN pipx install uv

ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

COPY package.json ./
RUN corepack enable && corepack prepare pnpm@latest --activate && pnpm install --prod --frozen-lockfile

COPY dist/ ./dist/
COPY prompts/ ./prompts/
COPY mcp-config.json ./

CMD ["node", "dist/handler.js"]
