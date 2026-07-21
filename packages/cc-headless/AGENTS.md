# CC Headless RCA Agent

Claude Code on Bedrock headless 모드를 사용하는 RCA 오케스트레이터입니다. ECS Fargate 컨테이너에서 SQS Long Polling으로 알람을 수신하고, CC CLI가 RCA → 조건부 Remediation → Report 전문 서브 에이전트를 순서대로 호출합니다.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.12 |
| Runtime | python:3.12-slim + Node.js 22 (CC CLI용) on ECS Fargate |
| Agent Engine | Claude Code CLI (headless, Bedrock backend) |
| MCP Tools | 읽기 전용 CloudWatch/CloudTrail/GitHub MCP, 산출물 저장 및 제한된 Healthcare reset MCP |
| Trigger | SQS Long Polling |
| Package Manager | uv |

## Directory Structure

```
src/cc_headless/
├── main.py              # ECS SQS long polling entry point
├── mcp_server.py        # 산출물 저장 + 서버 검증형 Healthcare reset
├── adapters/            # CC, DynamoDB, S3/SNS, S3 Vectors adapters
├── config/              # Environment variable configuration
├── ports/               # DTO와 port interfaces
└── services/            # Pipeline, prompt, artifact watcher, execution context
.claude/
├── agents/              # RCA, Remediation, Report 전문 에이전트
└── skills/              # 역할별 실행 가이드
prompts/
├── rca-system.md     # 루트 시스템 프롬프트 (include 지시자로 sections/ 조립)
├── rca-user.md       # 알람 정보 user prompt 템플릿
└── sections/         # 빌드 시 {{include: ...}}로 합성되는 프롬프트 조각
    ├── README.md         # 섹션 구조·편집 규칙
    ├── core/             # 공통 레이어 (artifacts-overview, pipeline-overview, principles)
    ├── artifacts/        # JSON 스키마 (RCA, remediation, playbook)
    └── stages/           # 전문 에이전트 호출 순서
mcp-config.json       # MCP server configuration for CC
Dockerfile            # ECS Fargate container image
pyproject.toml        # Python project configuration
```

## Dev Commands

```bash
uv sync --extra dev   # Install dependencies
uv run pytest tests/  # Run tests
uv run ruff check src/ tests/  # Lint
uv run ruff format src/ tests/ # Format
docker build -t cc-headless .  # Build container
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `SQS_QUEUE_URL` | SQS alarm queue URL |
| `SQS_POLL_WAIT_SECONDS` | Long polling wait (default: 20) |
| `CLAUDE_CODE_USE_BEDROCK` | `1` to enable Bedrock backend |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | Bedrock model ID |
| `DYNAMODB_TABLE_NAME` | Shared RCA session table |
| `S3_EVIDENCE_BUCKET` | Shared evidence bucket |
| `S3_REPORT_BUCKET` | Shared report bucket |
| `S3_VECTOR_BUCKET_NAME` | Shared S3 Vectors bucket |
| `SNS_NOTIFICATION_TOPIC_ARN` | Notification topic |
| `HEALTHCARE_SERVICE_HOST` | 허용된 Healthcare reset 대상의 Cloud Map host |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | GitHub MCP auth (optional) |
