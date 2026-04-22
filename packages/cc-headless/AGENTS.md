# CC Headless RCA Agent

Claude Code on Bedrock headless 모드를 사용하는 서버리스 RCA 에이전트입니다. Lambda 컨테이너에서 CC CLI를 subprocess로 호출하여 단일 프롬프트로 전체 RCA 워크플로우를 수행합니다.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Runtime | Node.js 22 (Lambda Container Image) |
| Agent Engine | Claude Code CLI (headless, Bedrock backend) |
| MCP Tools | CloudWatch MCP, CloudTrail MCP, GitHub MCP |
| Trigger | SQS Event Source Mapping |

## Directory Structure

```
src/
├── handler.ts        # Lambda SQS handler (entry point)
├── cc-runner.ts      # CC CLI subprocess wrapper
├── prompt-builder.ts # System + user prompt assembly
├── alarm-parser.ts   # CloudWatch SNS → AlarmContext
├── session-store.ts  # DynamoDB session management
└── report-store.ts   # S3 report storage + SNS notification
prompts/
├── rca-system.md     # RCA workflow system prompt
└── rca-user.md       # Alarm details user prompt template
mcp-config.json       # MCP server configuration for CC
Dockerfile            # Lambda container image
```

## Dev Commands

```bash
pnpm install
pnpm build          # TypeScript compile
pnpm test           # vitest
pnpm lint           # eslint
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `CLAUDE_CODE_USE_BEDROCK` | `1` to enable Bedrock backend |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | Bedrock model ID |
| `DYNAMODB_TABLE_NAME` | Shared RCA session table |
| `S3_EVIDENCE_BUCKET` | Shared evidence bucket |
| `S3_REPORT_BUCKET` | Shared report bucket |
| `S3_VECTOR_BUCKET_NAME` | Shared S3 Vectors bucket |
| `SNS_NOTIFICATION_TOPIC_ARN` | Notification topic |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | GitHub MCP auth (optional) |
