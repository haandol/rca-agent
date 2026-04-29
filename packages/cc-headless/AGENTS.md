# CC Headless RCA Agent

Claude Code on Bedrock headless 모드를 사용하는 RCA 에이전트입니다. ECS Fargate 컨테이너에서 SQS Long Polling으로 알람을 수신하고, CC CLI를 subprocess로 호출하여 단일 프롬프트로 전체 RCA 워크플로우를 수행합니다.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.12 |
| Runtime | python:3.12-slim + Node.js 22 (CC CLI용) on ECS Fargate |
| Agent Engine | Claude Code CLI (headless, Bedrock backend) |
| MCP Tools | CloudWatch MCP, CloudTrail MCP, GitHub MCP (Go binary) |
| Trigger | SQS Long Polling |
| Package Manager | uv |

## Directory Structure

```
src/cc_headless/
├── __init__.py
├── main.py           # ECS SQS long polling entry point
├── config.py         # Environment variable configuration
├── cc_runner.py      # CC CLI subprocess wrapper
├── prompt_builder.py # System + user prompt assembly
├── alarm_parser.py   # CloudWatch SNS → AlarmContext
├── session_store.py  # DynamoDB session management
├── report_store.py   # S3 report storage + SNS notification
└── healthz.py        # HTTP health check server
prompts/
├── rca-system.md     # 루트 시스템 프롬프트 (include 지시자로 sections/ 조립)
├── rca-user.md       # 알람 정보 user prompt 템플릿
└── sections/         # 빌드 시 {{include: ...}}로 합성되는 프롬프트 조각
    ├── README.md         # 섹션 구조·편집 규칙
    ├── core/             # 공통 레이어 (artifacts-overview, pipeline-overview, principles)
    ├── artifacts/        # JSON 스키마 (scoping, hypotheses, validation, playbook)
    └── stages/           # 11단계 개별 절차 (1-scoping ~ 11-verification)
tests/
├── test_alarm_parser.py
└── test_prompt_builder.py
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
| `GITHUB_PERSONAL_ACCESS_TOKEN` | GitHub MCP auth (optional) |
