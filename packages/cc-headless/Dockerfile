FROM node:22-slim

RUN apt-get update && apt-get install -y git curl && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

ADD https://astral.sh/uv/install.sh /tmp/uv-install.sh
RUN sh /tmp/uv-install.sh && rm /tmp/uv-install.sh

ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev

COPY dist/ ./dist/
COPY prompts/ ./prompts/
COPY .claude/ ./.claude/
COPY mcp-config.json ./
COPY CLAUDE.md ./

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["node", "-e", "fetch('http://localhost:8080/healthz').then(r=>{if(!r.ok)throw 1}).catch(()=>process.exit(1))"]

CMD ["node", "dist/main.js"]
