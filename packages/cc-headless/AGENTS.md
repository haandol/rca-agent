# CC Headless RCA Agent

Claude Code on Bedrock headless 모드를 사용하는 RCA 오케스트레이터입니다. 같은 이미지가
두 개의 독립적인 워커를 제공합니다.

| 워커 | 진입점 | 트리거 | 권한 |
|------|--------|--------|------|
| 분석 | `cc_headless.main` | 알람 큐 | **읽기 전용** |
| 실행 | `cc_headless.execution_main` | 실행 요청 큐 (사용자 승인) | 쓰기 |

분석 워커는 CC CLI로 RCA → Report 전문 에이전트를 호출해 플레이북을 포함한 단일 리포트를
만들고 종료합니다. 복구를 수행하지 않습니다. 실행 워커는 사용자가 대시보드에서 승인한
플레이북 절차를 수행하고, 해결이 확정되면 회고로 절차를 교정합니다.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.13 |
| Runtime | python:3.13-slim + Node.js 24 (CC CLI용) on ECS Fargate |
| Agent Engine | Claude Code CLI (headless, Bedrock backend) |
| MCP Tools (분석) | 읽기 전용 CloudWatch/CloudTrail/GitHub MCP, 산출물 저장 MCP |
| MCP Tools (실행) | 읽기 전용 CloudWatch MCP, 서버 판정형 명령 실행·증거 기록 MCP, 회고 갱신 MCP |
| Trigger | SQS Long Polling |
| Package Manager | uv |

## Directory Structure

```
src/cc_headless/
├── main.py                     # 분석 워커 — 알람 큐 long polling
├── execution_main.py           # 실행 워커 — 실행 요청 큐 long polling
├── mcp_server.py               # 분석 산출물 저장 (쓰기 도구 없음)
├── execution_mcp_server.py     # 서버 판정형 명령 실행 + 실행 증거 기록
├── retrospective_mcp_server.py # 회고 갱신안 저장
├── adapters/            # CC, DynamoDB, S3/SNS, S3 Vectors adapters
├── config/              # Environment variable configuration
├── ports/               # DTO와 port interfaces
└── services/            # Pipeline, prompt, artifact watcher, execution context
.claude/                 # 분석 하네스 — RCA, Report 전문 에이전트와 스킬
.claude-execution/       # 실행 하네스 — 실행·회고 에이전트
prompts/
├── rca-system.md     # 루트 시스템 프롬프트 (include 지시자로 sections/ 조립)
├── rca-user.md       # 알람 정보 user prompt 템플릿
└── sections/         # 빌드 시 {{include: ...}}로 합성되는 프롬프트 조각
    ├── README.md         # 섹션 구조·편집 규칙
    ├── core/             # 공통 레이어 (artifacts-overview, pipeline-overview, principles)
    ├── artifacts/        # JSON 스키마 (scoping, hypotheses, validation, playbook)
    └── stages/           # 전문 에이전트 호출 순서
mcp-config.json           # 분석 하네스 MCP 설정
execution-mcp-config.json # 실행 하네스 MCP 설정
CLAUDE.md                 # 분석 하네스 지침
EXECUTION.md              # 실행 하네스 지침
Dockerfile            # ECS Fargate container image
pyproject.toml        # Python project configuration
```

## 하네스 패리티

`.claude/`, `.claude-execution/`, `prompts/`, `CLAUDE.md`, `EXECUTION.md`,
`mcp-config.json`, `execution-mcp-config.json` 은 로컬 실행과 컨테이너 실행이 공유하는
단일 하네스다. 이미지는 이 자산을 변환 없이 담고, 로컬에서 검증한 하네스가 그대로
배포된다. MCP 설정에는 환경별 절대 경로를 넣지 않는다 —
`tests/test_prompt_contracts.py` 와 `tests/test_execution_harness_contracts.py` 가
이를 거부한다.

## 두 하네스의 권한 경계

분석과 실행은 도구를 공유하지 않는다. 분석 하네스에 쓰기 도구가 들어가면 사용자 승인
게이트가 무의미해지고, 실행 하네스가 분석 산출물 도구를 가지면 실행이 리포트를 변경할 수
있다. `tests/test_execution_harness_contracts.py` 가 양쪽을 모두 막는다.

**파괴적 액션 차단은 프롬프트가 아니라 서버가 수행한다.** 실행 도구가 명령을 argv 로
분해해 작업 이름을 추출하고 거부 어휘와 대조한 뒤에만 실행하며, 작업 이름을 확정할 수
없는 명령은 거부한다. 거부 어휘의 단일 소스는 `services/destructive_actions.py` 다.

**해결 판정의 권위도 서버에 있다.** 에이전트의 최종 서술이 아니라 서버가 기록한 관측이
실행 상태를 확정한다(`services/execution_outcome.py`).

## Dev Commands (실모델 의미 평가)

```bash
# 제공된 관측으로 실모델 의미 품질을 평가하고 정규화 결과 JSON 을 출력
# SQS 전달이나 실제 증거 탐색을 검증하는 배포 E2E 경로가 아니다.
uv run cc-headless-eval ../../tests/scenarios/rds-connection-pool-exhaustion.json
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

| Variable | Worker | Description |
|----------|--------|-------------|
| `SQS_QUEUE_URL` | 분석 | SQS alarm queue URL |
| `SQS_POLL_WAIT_SECONDS` | 분석 | Long polling wait (default: 20) |
| `EXECUTION_QUEUE_URL` | 실행 | 사용자 승인이 발행되는 실행 요청 큐 URL |
| `EXECUTION_POLL_WAIT_SECONDS` | 실행 | Long polling wait (default: 20) |
| `EXECUTION_TIMEOUT_SECONDS` | 실행 | 실행 하네스 상한 (default: 3600) |
| `RETROSPECTIVE_TIMEOUT_SECONDS` | 실행 | 회고 하네스 상한 (default: 900) |
| `EXECUTION_COMMAND_TIMEOUT_SECONDS` | 실행 | 개별 명령 상한 (default: 300) |
| `CLAUDE_CODE_USE_BEDROCK` | 공통 | `1` to enable Bedrock backend |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | 공통 | Bedrock model ID |
| `DYNAMODB_TABLE_NAME` | 공통 | Shared RCA session table |
| `S3_EVIDENCE_BUCKET` | 공통 | 증거 원본 · 실행 증거 · 갱신 전 플레이북 사본 |
| `S3_REPORT_BUCKET` | 분석 | Shared report bucket |
| `S3_VECTOR_BUCKET_NAME` | 공통 | Shared S3 Vectors bucket |
| `SNS_NOTIFICATION_TOPIC_ARN` | 분석 | Notification topic |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | 분석 | GitHub MCP auth (optional) |

`EXECUTION_CLAIM_SECONDS` 는 `EXECUTION_TIMEOUT_SECONDS + 900` 미만으로 내려가지
않는다. claim 이 최악 실행 시간보다 짧으면 실행 중인 요청이 재전달되어 중복 실행된다.
