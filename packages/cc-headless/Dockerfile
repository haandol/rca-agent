FROM node:22-slim

RUN apt-get update && apt-get install -y git curl && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

ARG GITHUB_MCP_VERSION=v1.0.2
RUN ARCH=$(dpkg --print-architecture | sed 's/amd64/x86_64/;s/arm64/arm64/') && \
    curl -fsSL "https://github.com/github/github-mcp-server/releases/download/${GITHUB_MCP_VERSION}/github-mcp-server_Linux_${ARCH}.tar.gz" | tar -xz -C /usr/local/bin github-mcp-server && \
    chmod +x /usr/local/bin/github-mcp-server

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

RUN useradd -m -s /bin/bash ccuser \
    && chown -R ccuser:ccuser /app \
    && cp -r /root/.local /home/ccuser/.local \
    && chown -R ccuser:ccuser /home/ccuser/.local

USER ccuser
ENV PATH="/home/ccuser/.local/bin:$PATH"
ENV HOME="/home/ccuser"

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["node", "-e", "fetch('http://localhost:8080/healthz').then(r=>{if(!r.ok)throw 1}).catch(()=>process.exit(1))"]

CMD ["node", "dist/main.js"]
