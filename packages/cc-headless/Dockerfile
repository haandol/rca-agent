FROM public.ecr.aws/lambda/nodejs:22

RUN dnf install -y git tar gzip && dnf clean all

RUN npm install -g @anthropic-ai/claude-code

ADD https://astral.sh/uv/install.sh /tmp/uv-install.sh
RUN sh /tmp/uv-install.sh && rm /tmp/uv-install.sh

ENV PATH="/root/.local/bin:$PATH"

COPY package.json ./
RUN npm install --omit=dev

COPY dist/ ./dist/
COPY prompts/ ./prompts/
COPY mcp-config.json ./
COPY .mcp.json ./
COPY CLAUDE.md ./

CMD ["dist/handler.handler"]
