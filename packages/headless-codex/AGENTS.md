# Headless Codex RCA Agent

Codex CLI를 Amazon Bedrock Runtime Global Inference Profile에 연결한 RCA
오케스트레이터입니다. 같은 이미지가
두 개의 독립적인 워커를 제공합니다.

| 워커 | 진입점 | 트리거 | 권한 |
|------|--------|--------|------|
| 분석 | `headless_codex.main` | 알람 큐 | **읽기 전용** |
| 실행 | `headless_codex.execution_main` | 실행 요청 큐 (사용자 승인) | 쓰기 |

분석 워커는 Codex CLI로 RCA → Report 전문 에이전트를 호출해 플레이북을 포함한 단일 리포트를
만들고 종료합니다. 복구를 수행하지 않습니다. 실행 워커는 사용자가 대시보드에서 승인한
플레이북 절차를 수행하고, 해결이 확정되면 회고로 절차를 교정합니다.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.13 |
| Runtime | python:3.13-slim + Node.js 24 (Codex CLI용) on ECS Fargate |
| Agent Engine | Codex CLI (`codex exec`, Bedrock Runtime Responses API) |
| Model | `global.openai.gpt-5.6-sol`, reasoning effort `high` |
| MCP Tools (분석) | 읽기 전용 CloudWatch/CloudTrail/GitHub MCP, RCA 전용 ECS 관측 조회와 산출물 저장 MCP |
| MCP Tools (실행) | 읽기 전용 CloudWatch MCP, 서버 판정형 명령 실행·증거 기록 MCP, 회고 갱신 MCP |
| Trigger | SQS Long Polling |
| Package Manager | uv |

## Directory Structure

```
src/headless_codex/
├── main.py                     # 분석 워커 — 알람 큐 long polling
├── execution_main.py           # 실행 워커 — 실행 요청 큐 long polling
├── mcp_server.py               # 분석 산출물 저장·관측된 ECS 태스크 조회 (변경 도구 없음)
├── execution_mcp_server.py     # 서버 판정형 명령 실행 + 실행 증거 기록
├── retrospective_mcp_server.py # 회고 갱신안 저장
├── adapters/            # Codex, DynamoDB, S3/SNS, S3 Vectors adapters
├── config/              # Environment variable configuration
├── ports/               # DTO와 port interfaces
└── services/            # Pipeline, prompt, artifact watcher, execution context
harness/
├── analysis/            # 분석 루트 지침, 전문 에이전트 설정, model-eval 설정
├── comparison/          # 도구 없는 플레이북 비교 프로필
├── execution/           # 승인 실행 루트 지침과 execution-operator 설정
├── retrospective/       # 회고 루트 지침과 retrospective-analyst 설정
└── skills/              # 분석·보고 절차 스킬
prompts/
├── rca-system.md     # 루트 시스템 프롬프트 (include 지시자로 sections/ 조립)
├── rca-user.md       # 알람 정보 user prompt 템플릿
└── sections/         # 빌드 시 {{include: ...}}로 합성되는 프롬프트 조각
    ├── README.md         # 섹션 구조·편집 규칙
    ├── core/             # 공통 레이어 (artifacts-overview, pipeline-overview, principles)
    ├── artifacts/        # JSON 스키마 (scoping, hypotheses, validation, playbook)
    └── stages/           # 전문 에이전트 호출 순서
Dockerfile            # ECS Fargate container image
pyproject.toml        # Python project configuration
```

분석 산출물은 초기 `hypotheses.json`을 보존하고, 서버가 재생성을 요청한 경우
`hypotheses-2.json`, `hypotheses-3.json`을 추가합니다. validation은 1부터 연속하며
최대 3개입니다. 저장 서버가 Strands와 같은 단계 상태, 신뢰도 재분류, 종료 판정을
강제하고 최종 리포트의 근본 원인·대응 플레이북 섹션을 구조화 산출물에서 렌더링합니다.

validation 저장 응답의 `effective_state`와 Report 입력은 같은 서버 replay 결과를 사용합니다.
기존 가설의 실제 판정·증거를 전달하며 `CLOSED`는 기각으로 해석하지 않습니다. 평가의
근본원인 유형·인용도 마지막 파일의 첫 confirmed 항목이 아니라 서버가 선택한 유효 판정을
사용합니다. 빔 3, 기존 우선순위, 신뢰도와 검증 상한은 바꾸지 않습니다.

원본 `AlarmDescription`은 선택 문자열로 보존해 외부 데이터로 표시합니다. 설명의 정적
좌표는 탐색 단서이며 지시·권한·현재 소유권 증명이 아닙니다. 실행 입력의 원본 알람 JSON도
설명이나 알 수 없는 필드를 잘라내지 않습니다.

production RCA는 차단 PID·트랜잭션 시작 시각·runId/application_name을 소유자 lifecycle 이벤트에
먼저 연결하고, 그 이벤트의 로그 스트림에서 `ecs_runtime_identity`를 찾습니다.
서비스 관측자의 `db_wait_snapshot.activity[]`에 차단 PID가 보인다는 이유로 관측자의
서비스 태스크를 차단자에 연결하지 않습니다. `inspect_ecs_task_control`은 이렇게 관측한 task/cluster
ARN만 받아 현재 세션의 알람 계정·리전과 대조하고 `DescribeTasks(include=["TAGS"])`
결과를 제한해 반환합니다. 관측된 taskDefinitionArn을 보존하고 family/revision은
`task_definition_arn_derived`로 출처를 표시합니다. 태스크 정의 자체는 조회하지 않습니다.
RCA는 반환된 태스크 식별자·소유 run/journal 태그를 앞서 연결한 소유자 이벤트와 대조합니다.
Report·model-eval·실행 프로필에는 노출하지 않습니다. 컨테이너 환경 변수·secrets·command·
overrides는 반환하지 않으며 민감 태그 값을 가리고 소유 run/journal 태그는 보존합니다.
선택 원인의 인과 증거와 함께 강한 validation 저장 전에 수집하되, 제어 정보 누락은
인과 확정을 막지 않고 수동 계획으로 전달합니다. 검증 루프·종료·승인 gate는 그대로입니다.

필요한 ECS 권한은 대상 태스크의 `DescribeTasks`와 태그 조회용 `ListTagsForResource`이며
권한 부여는 Infra가 담당합니다. 이 도구는 IAM을 변경하지 않고, 조회 거부 시 실패로 반환하며
소유권을 추정하지 않습니다.

## 하네스 패리티

`harness/`와 `prompts/`는 로컬 실행과 컨테이너 실행이 공유하는 단일 하네스다.
이미지는 이 자산을 그대로 담고, 러너는 실행별 격리된 `CODEX_HOME`에 절대 경로를
렌더링한다. 커밋된 설정에는 환경별 절대 경로를 넣지 않는다 —
`tests/test_prompt_contracts.py` 와 `tests/test_execution_harness_contracts.py` 가
이를 거부한다.

## 플레이북 비교와 게시

분석 산출물 검증 뒤 `comparison` 프로필이 게시된 플레이북 상세와 이번 사고의 증거를
비교합니다. 이 프로필은 도구·셸·웹 검색·다른 에이전트를 사용하지 않으며, 분석용
산출물 쓰기 토큰과 스킬을 전달받지 않습니다. 비교 준비와 모델 호출은 분석에서 시작한
동일한 종료 시한을 공유합니다. 취소되거나 남은 시간이 없으면 새 예산을 부여하지 않습니다.

비교 모델은 JSON 판단만 반환합니다. 서버가 출력 형식과 중복 키, 제공된 증거와의 연결을
검증하고 기준 사본·변경 전후·제안 상태를 구성합니다. 새 사고의 지식 변경 제안은 사용자가
반영하기 전까지 기존 공개 지식을 바꾸지 않습니다. 지식 반영과 이번 사고 런북의 실행
승인은 별개의 행동입니다.

공개 라이브러리의 head는 현재 개정본과 게시 상태를 보유하며, 게시 완료된 개정본만
검색에 사용합니다. 개정본별 snapshot은 내용을 고정한 사본입니다. 이번 사고의 런북은
현재 대상·명령·순서·성공 기준을 보유하고, 실행은 사용자가 승인한 사본을 따릅니다.
공개 지식을 참조했다는 이유로 과거 사고의 명령을 상속하지 않습니다.

해결된 실행 직후의 회고는 사용자 지식 반영과 별도로 자동 수행합니다. 회고는 승인된
사고 런북과 그때 참조한 공개 개정본의 snapshot을 구분합니다. 공개 기준이 바뀌면 갱신을
거부하고, 일치하면 새 회고 개정본을 준비한 뒤 원래 회고 개정본의 커밋과 공개 head의
조건부 교체를 순서대로 수행합니다. 최종 게시만 남은 재전달은 실행이나 모델 회고를
반복하지 않고 게시를 복구합니다.

## 하네스의 권한 경계

분석과 실행은 도구를 공유하지 않는다. 분석 하네스에 쓰기 도구가 들어가면 사용자 승인
게이트가 무의미해지고, 실행 하네스가 분석 산출물 도구를 가지면 실행이 리포트를 변경할 수
있다. `tests/test_execution_harness_contracts.py` 가 양쪽을 모두 막는다.

**파괴적 액션 차단은 프롬프트가 아니라 서버가 수행한다.** 실행 도구가 명령을 argv 로
분해해 작업 이름을 추출하고 거부 어휘와 대조한 뒤에만 실행하며, 작업 이름을 확정할 수
없는 명령은 거부한다. 거부 어휘의 단일 소스는 `services/destructive_actions.py` 다.

**해결 판정의 권위도 서버에 있다.** 에이전트의 최종 서술이 아니라 서버가 기록한 관측이
실행 상태를 확정한다(`services/execution_outcome.py`).

분석과 Report는 `services/execution_capabilities.py`가 기존 명령 gate에서 계산한 실행
능력을 전달받습니다. 이는 도구나 권한 추가가 아닙니다. 소유권과 롤백 경로가 확인된 독립
태스크의 `stop-task`와 영구 인스턴스 종료를 구분하며 직접 SQL·셸·ECS exec는 허용하지 않습니다.
실행 전용 `wait_for_post_action_metrics`는 승인된 검증 절차와 앞선 조치 절차를 받아 같은
실행의 첫 성공 실제 StopTask ended_at을 UTC 초 epoch로 변환한 뒤
floor(epoch/60)*60+60부터 첫 두 완결된 60초 구간을 고정합니다. 정확한 분 경계에
끝나도 반드시 다음 분부터 시작하며 조회 전에 앵커와 구간을 보존합니다.
현재 검증 절차의 `run_playbook_command`로 list-metrics·describe-alarms를 먼저 기록하고,
승인된 현재 서비스의 실제 좌표·알람 기준으로 읽기 전용 조회를 수행합니다.
최대 900초를 기존 실행 전체 3600초 강제 기한과 MCP timeout 1200초 안에서 기다리며 명령 gate·감사 기록,
취소·claim 경계를 유지합니다. 동일 요청은 최종 영수증을 재사용하고 비정상 구간을
나중 정상 구간으로 바꾸지 않습니다. metrics의 attempts/failures는 필수이며 latency와
latency_alarm_name은 승인 기준에 지연 지표와 알람이 명시된 경우에만 추가합니다.
쓰기 성공·실패 0·알람 OK 기준에는 선택 지연 메타데이터가 없어도 사용할 수 있습니다.
attempts-failures는 산술 차이입니다. successful_writes는 서버가 완료된 쓰기 집계의
의미를 실제 증거에 연결했을 때만 제공하며, 없으면 모델이 실제 쓰기 성공을 별도 확인합니다.
선택 completed_work_evidence의 소스 인용은 실제 명령 증거나 승인 문맥을 참조해야 합니다.
생산자/소스/관측에서 의미를 발견하며 이름만 보고 쓰기로 추정하거나 좌표·증거·집계 수를
만들지 않습니다. 관측된 operation=ingest 쓰기와 operation=patient_vitals 읽기의 구분은
해당 관측에만 적용하며 고정 식별자로 사용하지 않습니다. 읽기 성공은 쓰기 성공이 아닙니다.
각 명령 완료 후 observedAt으로 구간 완결을 판정하고 다른 지표가 누락되어도 완결된
구간의 실패는 재시도 전에 종결합니다. 과거 로그는 실제 사고 시간 범위를 지정하고,
알려진 소유자 스트림을 먼저 조회합니다. 페이지 토큰을 따라 완결 여부를 기록하고,
예산 내 미완료·출력 잘림을 부재로 해석하거나 조용히 건수를 제한하지 않습니다. waiter 조기 OK나 영수증만으로 해결을 추정하지 않고 정확한 소유자의
해제·롤백과 승인 성공 기준은 별도 확인합니다.

## Dev Commands (실모델 의미 평가)

```bash
# 제공된 관측으로 실모델 의미 품질을 평가하고 정규화 결과 JSON 을 출력
# SQS 전달이나 실제 증거 탐색을 검증하는 배포 E2E 경로가 아니다.
uv run headless-codex-eval ../../tests/scenarios/rds-connection-pool-exhaustion.json
```

## Dev Commands

```bash
uv sync --extra dev   # Install dependencies
uv run pytest tests/  # Run tests
uv run ruff check src/ tests/  # Lint
uv run ruff format src/ tests/ # Format
docker build -t headless-codex .  # Build container
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
| `AWS_REGION` | 공통 | Bedrock Runtime source region |
| `CODEX_MODEL` | 공통 | 반드시 `global.openai.gpt-5.6-sol` |
| `CODEX_REASONING_EFFORT` | 공통 | 반드시 `high` |
| `CODEX_MODEL_PROVIDER` | 공통 | 반드시 `amazon-bedrock-runtime` |
| `CODEX_BEDROCK_BASE_URL` | 공통 | Bedrock Runtime OpenAI-compatible endpoint |
| `CODEX_RUNTIME_HOME_ROOT` | 공통 | 실행별 `CODEX_HOME` 상위 디렉터리 (`/tmp` 사용 금지) |
| `CODEX_TIMEOUT_SECONDS` | 분석 | 분석 하네스 상한 (default: 3600) |
| `DYNAMODB_TABLE_NAME` | 공통 | Shared RCA session table |
| `S3_EVIDENCE_BUCKET` | 공통 | 증거 원본 · 실행 증거 · 갱신 전 플레이북 사본 |
| `S3_REPORT_BUCKET` | 분석 | Shared report bucket |
| `S3_VECTOR_BUCKET_NAME` | 공통 | Shared S3 Vectors bucket |
| `SNS_NOTIFICATION_TOPIC_ARN` | 분석 | Notification topic |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | 분석 | GitHub MCP auth (optional) |

`EXECUTION_CLAIM_SECONDS` 는 `EXECUTION_TIMEOUT_SECONDS + 900` 미만으로 내려가지
않는다. claim 이 최악 실행 시간보다 짧으면 실행 중인 요청이 재전달되어 중복 실행된다.
