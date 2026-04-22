# RCA Agent Project Guide

RCA Agent는 AWS 기반 자동 RCA(근본원인분석) 에이전트 시스템의 Nx 모노레포(pnpm workspace)입니다.

## Repository Structure

| Package | Description | Tech |
|---------|-------------|------|
| [`packages/agent`](./packages/agent/) | Strands Agents SDK 기반 RCA 에이전트 — 가설 생성기(Orchestrator) + 툴-콜러(Tool-Caller) | Python, Strands Agents SDK, Amazon Bedrock |
| [`packages/infra`](./packages/infra/) | AWS CDK 인프라 — ECS Fargate, SNS/SQS, DynamoDB, S3, VPC | TypeScript, CDK |
| [`packages/web`](./packages/web/) | RCA 대시보드 웹 프론트엔드 — RCA 목록, 가설 트리, 증거 패널, 보고서 뷰 | TypeScript, Nuxt 4, TailwindCSS, DaisyUI |
| [`packages/healthcare-sensor-app`](./packages/healthcare-sensor-app/) | 헬스케어 센서 데이터 수집/조회 서비스 — RCA 에이전트 검증용 장애 주입 지원 | Python, FastAPI, SQLAlchemy, OpenTelemetry |

## Quick Start

Prerequisites와 환경 설정은 각 패키지의 AGENTS.md를 참조하세요.

```bash
pnpm install
pnpm nx run-many -t build
pnpm nx run-many -t test
```

## Agent Work Protocol

### Development Cycle

```
1. Review/Create ADR → 2. Implement feature → 3. Build/lint verification → 4. Test → 5. Sync ADR → 6. Commit
```

- **New feature**: 관련 ADR을 먼저 읽거나 새로 작성한 후 구현을 시작합니다.
- **Bug fix**: ADR 업데이트 불필요 (아키텍처 변경이 없는 경우).
- **Before commit**: 구현이 ADR과 달라졌으면 ADR과 `docs/adr/README.md` 인덱스를 반드시 업데이트합니다.
- **Rollback**: 빌드/테스트 실패 시 `git stash` 또는 `git checkout -- <file>`로 복원. `git reset --hard`나 force push는 사용자 확인 없이 실행하지 않습니다.

### Principles

- 한 번에 하나의 기능/버그에 집중
- 큰 변경은 원자적 커밋으로 분리 ([CONTRIBUTING.md](./CONTRIBUTING.md) 참조)
- 세션 종료 시 코드는 빌드 가능하고 린트를 통과해야 함
- `git log`만으로 진행 상황을 파악할 수 있도록 서술적 커밋 메시지 작성
- 아키텍처 결정 변경 시 ADR 업데이트, 단순 버그 수정이나 스타일 변경은 생략
- Early return 패턴 선호: 에러와 엣지 케이스를 먼저 처리한 후 메인 로직 수행

### Sub-Agent Delegation

이 모노레포는 패키지별로 기술 스택이 다릅니다. **메인 에이전트가 오케스트레이터 역할을 하고, 패키지별 작업은 서브 에이전트에게 위임합니다.** 각 서브 에이전트는 자기 패키지의 AGENTS.md를 읽고, 해당 디렉토리에서 명령을 실행하며, 다른 패키지의 패턴을 적용하지 않습니다.

#### Sub-Agent Definitions

| Sub-Agent | Directory | Language | Lint/Build |
|-----------|-----------|----------|------------|
| **Agent** | `packages/agent/` | Python | `ruff check`, `pytest` |
| **Infra** | `packages/infra/` | TypeScript (CDK) | `pnpm lint`, `pnpm build`, `pnpm test` |
| **Web** | `packages/web/` | TypeScript (Nuxt) | `pnpm lint`, `pnpm build` |
| **Healthcare Sensor App** | `packages/healthcare-sensor-app/` | Python (FastAPI) | `ruff check`, `pytest` |

#### Orchestrator Responsibilities

1. **Plan** — ADR 읽기, 범위 정의, 영향받는 패키지 식별
2. **Define API contract** — 패키지 간 기능의 경우 인터페이스(이벤트 페이로드, DynamoDB 스키마, S3 경로 규칙 등) 사전 정의
3. **Delegate** — 각 서브 에이전트에게 계약과 제약 조건을 포함한 명확한 태스크 전달
4. **Integrate** — 통합 변경 사항 검토 후 커밋

#### Cross-Package Development

기본 순서: **Infra → Agent → Web** (의존성 하향). 각 패키지를 완료하고 검증한 후 다음으로 진행합니다. API 계약을 컨텍스트로 전달하여 서브 에이전트가 호환 가능한 인터페이스를 독립적으로 구현합니다.

## Architecture Overview

### System Architecture

```mermaid
graph TB
    subgraph EventSource["이벤트 소스"]
        CW_ALARM["☁️ CloudWatch Alarm"]
    end

    subgraph Messaging["이벤트 라우팅"]
        SNS_IN["SNS Topic<br/>(알람 팬아웃)"]
        SQS["SQS Queue<br/>(Long Polling)"]
    end

    subgraph Compute["에이전트 실행"]
        ECS["ECS Fargate<br/>RCA Agent (main.py)"]
    end

    subgraph LLM["LLM 추론"]
        BEDROCK["Amazon Bedrock<br/>Claude Sonnet 4.6<br/>(non-streaming, structured output)"]
    end

    subgraph DataTools["데이터 수집 도구"]
        CW_MCP["CloudWatch MCP Server<br/>(awslabs.cw-mcp-server)"]
        CW_API["CloudWatch<br/>Metrics / Logs"]
    end

    subgraph Storage["영속 저장소"]
        S3_VECTORS["S3 Vectors<br/>(플레이북 임베딩)"]
        S3["S3 Bucket<br/>(증거 / 보고서)"]
        DDB["DynamoDB<br/>(가설 트리 상태)"]
    end

    subgraph Notification["알림"]
        SNS_OUT["SNS Topic<br/>(RCA 완료 알림)"]
        SRE["👩‍💻 SRE / Ops 팀"]
    end

    CW_ALARM --> SNS_IN --> SQS --> ECS
    ECS <--> BEDROCK
    ECS --> CW_MCP --> CW_API
    ECS <--> S3_VECTORS
    ECS --> S3
    ECS --> DDB
    ECS --> SNS_OUT --> SRE
```

### Agent Pipeline (State Machine)

에이전트는 가설-검증 루프를 반복하며, 5가지 종료 조건(OR) 중 하나라도 만족하면 종료합니다.

```mermaid
stateDiagram-v2
    [*] --> ALARM_RECEIVED: SQS 메시지 수신

    ALARM_RECEIVED --> SCOPING: AlarmPayload 파싱
    note right of SCOPING
        CloudWatch MCP로 메트릭 수집
        S3 Vectors에서 유사 플레이북 검색
        timeout: 300s
    end note

    SCOPING --> HYPOTHESIS_GENERATION: ScopingResult
    note right of HYPOTHESIS_GENERATION
        초기 가설 3~5개 생성 (depth=0)
        재시도: 최대 3회
        timeout: 180s/회
    end note

    HYPOTHESIS_GENERATION --> HYPOTHESIS_PRIORITIZATION: list[Hypothesis]

    HYPOTHESIS_PRIORITIZATION --> EVIDENCE_COLLECTION: PrioritizationResult
    note right of EVIDENCE_COLLECTION
        TODO: 미구현 (F5-F9)
        CloudWatch 메트릭/로그 수집
        CloudTrail 배포 이력 확인
    end note

    EVIDENCE_COLLECTION --> HYPOTHESIS_VALIDATION: evidence_map
    note right of HYPOTHESIS_VALIDATION
        가설별 confidence 재평가
        3-tier 분류:
        ≥0.8 CONFIRMED
        ≤0.3 REJECTED
        그 외 NEEDS_INVESTIGATION
    end note

    HYPOTHESIS_VALIDATION --> TERMINATION_CHECK: ValidationResult

    state TERMINATION_CHECK <<choice>>
    TERMINATION_CHECK --> BRANCHING: 계속 탐색
    TERMINATION_CHECK --> REPORT_GENERATION: should_terminate=true

    note left of TERMINATION_CHECK
        종료 조건 (OR):
        1. confidence ≥ 0.9 (CONFIRMED)
        2. 시간 ≥ 20분
        3. tree depth > 5
        4. 검증 루프 > 3회
        5. 전체 가설 REJECTED
    end note

    BRANCHING --> HYPOTHESIS_PRIORITIZATION: 새 하위 가설 추가
    note right of BRANCHING
        NEEDS_INVESTIGATION 가설 분기
        중복 제거 (부모/rejected)
        max_depth=3
    end note

    REPORT_GENERATION --> PLAYBOOK_GENERATION: RcaReport
    note right of REPORT_GENERATION
        인시던트 요약 / 근본 원인
        임시 완화 / 영구 해결
        타임라인 구성
        S3에 Markdown 저장
    end note

    PLAYBOOK_GENERATION --> NOTIFICATION: Playbook
    note right of PLAYBOOK_GENERATION
        RCA 결과에서 플레이북 생성
        S3 Vectors에 인덱싱
        (다음 인시던트에서 검색 가능)
    end note

    NOTIFICATION --> COMPLETED: SNS 발행
    note right of NOTIFICATION
        presigned URL 생성
        SNS 발행 (backoff 재시도 3회)
    end note

    COMPLETED --> [*]
```

### Data Flow (모듈 간 데이터 흐름)

각 모듈이 생산/소비하는 Pydantic 모델과 모듈 간 의존 관계를 나타냅니다.

```mermaid
flowchart TD
    subgraph Input["입력 파싱 (main.py)"]
        SQS_MSG["SQS Message (JSON)"]
        SNS_PARSE["_parse_sns_envelope()"]
        AP["AlarmPayload"]
        SQS_MSG --> SNS_PARSE --> AP
    end

    subgraph F1["F1: Scoping (scoping.py)"]
        direction TB
        PB_SEARCH["search_similar_playbooks()<br/>S3 Vectors → PlaybookMatch[]"]
        SCOPING_AGENT["Scoping Agent<br/>(CloudWatch MCP)"]
        SO["ScopingOutput<br/>(structured_output)"]
        SR["ScopingResult"]
        PB_SEARCH --> SCOPING_AGENT
        SCOPING_AGENT --> SO --> SR
    end

    subgraph F2["F2: Hypothesis Generation (hypothesis.py)"]
        direction TB
        HYP_AGENT["Hypothesis Agent"]
        HO["HypothesisOutput<br/>(structured_output)"]
        HGR["HypothesisGenerationResult"]
        H["Hypothesis[]<br/>(tree_id, depth=0)"]
        HYP_AGENT --> HO --> HGR
        HGR --> H
    end

    subgraph F3["F3: Prioritization (prioritization.py)"]
        direction TB
        PRIO_AGENT["Prioritization Agent"]
        PO["PrioritizationOutput<br/>(structured_output)"]
        PR["PrioritizationResult"]
        PH["PrioritizedHypothesis[]<br/>(rank, tools, parallel_group)"]
        PRIO_AGENT --> PO --> PR
        PR --> PH
    end

    subgraph F4["F4: Validation (validation.py)"]
        direction TB
        VAL_AGENT["Validation Agent"]
        VO["ValidationOutput<br/>(structured_output)"]
        VJ["ValidationJudgment[]"]
        VR["ValidationResult<br/>(all_rejected flag)"]
        VAL_AGENT --> VO --> VJ --> VR
    end

    subgraph F5["F5: Branching (branching.py)"]
        direction TB
        BR_AGENT["Branching Agent"]
        BO["BranchingOutput<br/>(structured_output)"]
        BR["BranchingResult"]
        CH["Child Hypothesis[]<br/>(depth=parent+1)"]
        BR_AGENT --> BO --> BR
        BR --> CH
    end

    subgraph F6["F6: Termination (termination.py)"]
        direction TB
        TC["check_termination()<br/>(순수 로직, LLM 미사용)"]
        TD_OUT["TerminationDecision<br/>(should_terminate, reason,<br/>best_hypothesis)"]
        TC --> TD_OUT
    end

    subgraph F7["F7: Report (report.py)"]
        direction TB
        RPT_AGENT["Report Agent"]
        RO["ReportOutput<br/>(structured_output)"]
        RCA["RcaReport"]
        S3_SAVE["save_report_to_s3()<br/>→ Markdown"]
        RPT_AGENT --> RO --> RCA --> S3_SAVE
    end

    subgraph F8["F8: Playbook (playbook_gen.py)"]
        direction TB
        PBK_AGENT["Playbook Agent"]
        PBO["PlaybookOutput<br/>(structured_output)"]
        PBK["Playbook"]
        S3V_SAVE["save_playbook_to_s3_vectors()<br/>→ S3 Vectors 인덱싱"]
        PBK_AGENT --> PBO --> PBK --> S3V_SAVE
    end

    subgraph F9["F9: Notification (notification.py)"]
        direction TB
        BUILD_N["build_notification()"]
        NM["NotificationMessage"]
        SEND_N["send_notification()<br/>→ SNS Publish"]
        BUILD_N --> NM --> SEND_N
    end

    AP --> F1
    SR --> F2
    H --> F3
    SR --> F3
    PH --> F4
    VR --> F6
    H --> F6
    VJ -->|NEEDS_INVESTIGATION| F5
    TD_OUT -->|should_terminate=true| F7
    CH -->|새 가설 추가| F3
    RCA --> F8
    RCA --> F9

    style F6 fill:#f9f3e3,stroke:#d4a843
    style Input fill:#e3f2fd,stroke:#1976d2
```

### Agent Architecture

- **Supervisor-Orchestrator 패턴**: 가설 생성기(Orchestrator Agent)가 전문 툴 에이전트(Tool-Caller)에게 작업 위임
- **가설-트리 탐색**: 증거에 따라 가지치기/확장하는 트리형 점진적 추론
- **상태 머신**: `ALARM_RECEIVED` → `SCOPING` → `HYPOTHESIS_GENERATION` → `HYPOTHESIS_PRIORITIZATION` → `EVIDENCE_COLLECTION` → `HYPOTHESIS_VALIDATION` → `REPORT_GENERATION` → `COMPLETED`

### Technology Stack

| Component | Technology |
|-----------|-----------|
| 에이전트 프레임워크 | Strands Agents SDK |
| 에이전트 실행 환경 | AWS ECS Fargate |
| 이벤트 라우팅 | Amazon SNS + SQS |
| LLM 추론 | Amazon Bedrock (Claude) |
| 임베딩 | Amazon Bedrock Cohere Embed v4 |
| 메트릭/로그 도구 | AWS Labs CloudWatch MCP 서버 (`awslabs/cloudwatch-mcp-server`) |
| 배포 이력 도구 | AWS Labs CloudTrail MCP 서버 (`awslabs/cloudtrail-mcp-server`) |
| 코드 변경 분석 도구 | GitHub MCP 서버 (`github/github-mcp-server`) |
| 분산 트레이스 | ADOT + AWS X-Ray (MVP 이후) |
| 증거/보고서 저장 | Amazon S3 |
| 벡터 검색 | Amazon S3 Vectors |
| 가설 트리 상태 | Amazon DynamoDB |
| 시크릿 관리 | AWS Secrets Manager |
| 알림 | Amazon SNS |
| 네트워크 보안 | VPC + PrivateLink |

## Architecture Decision Records

`docs/adr/` — 새로운 기능이나 주요 변경 시 ADR 작성이 필수입니다. ADR은 **한국어**로 작성합니다. 전체 인덱스: **[docs/adr/README.md](./docs/adr/README.md)**

### ADR Workflow

#### Before Implementation (Required)

1. **Check existing ADRs** — `docs/adr/README.md` 인덱스에서 관련 ADR 확인
2. **Create or review ADR**
   - 관련 ADR이 없으면 → `docs/adr/TEMPLATE.md` 기반으로 새 ADR 작성 (status: `Proposed`)
   - 관련 ADR이 있으면 → 읽고 현재 구현 방향과 일치하는지 확인
3. **Scope implementation to ADR** — ADR에 기술된 결정을 따라 구현

#### After Implementation (Required)

1. **Sync ADR** — 아키텍처 결정 자체가 변경되었으면 ADR 업데이트 (status → `Accepted`). 구현 세부사항(파일 경로, 코드 스니펫, DB 필드 스키마)은 ADR에 넣지 않음
2. **Update README index** — `docs/adr/README.md` 인덱스를 최신 상태로 유지
3. **Cascade updates** — 변경이 다른 ADR에 영향을 주면 해당 ADR도 업데이트

#### When ADR is Not Required

- 단순 버그 수정 (아키텍처 변경 없음)
- 스타일/포매팅 변경
- 문서 오타 수정
- 의존성 패치 버전 업데이트

## Documentation Maintenance

- ADR 인덱스 (`docs/adr/README.md`) 동기화 유지
- 주요 기능 추가나 프로젝트 구조 변경 시 관련 AGENTS.md 업데이트
- DynamoDB 스키마 변경 시 관련 문서 업데이트
- 프롬프트 변경 시 시나리오 테스트셋으로 정확도 검증

## Reference Documents

| Document | Description |
|----------|-------------|
| [PRD](./docs/prd/aws-rca-agent-prd.md) | 제품 요구사항 정의서 — 기능 명세, 데모 시나리오, KPI |
| [ADR Index](./docs/adr/README.md) | 아키텍처 결정 기록 인덱스 |
| [Contributing Guide](./CONTRIBUTING.md) | 커밋 메시지, 브랜치 전략, PR 규칙 |

## Deployment

### Infrastructure (CDK)

```bash
cd packages/infra
pnpm nx deploy infra
```

### Agent (ECS Fargate)

에이전트는 ECS Fargate 태스크로 배포됩니다. SQS 큐를 Long Polling으로 구독하며, 알람 메시지 수신 시 RCA 워크플로우를 자동 시작합니다.

### Web Dashboard

```bash
pnpm nx build web
```

## Testing

```bash
# 전체 테스트
pnpm nx run-many -t test

# 특정 패키지 테스트
pnpm nx test agent
pnpm nx test infra

# 영향받은 프로젝트만 테스트
pnpm nx affected -t test
```

### RCA 정확도 테스트

에이전트의 RCA 정확도는 시나리오 테스트셋(과거 실제 인시던트 재현 케이스)으로 측정합니다:
- **Precision**: 에이전트가 제시한 근본 원인이 실제 원인과 일치하는 비율 (목표 90%+)
- **Recall**: 실제 원인이 에이전트의 가설 목록에 포함되는 비율 (목표 90%+)
- **오탐율**: 정상 상태에서 에이전트가 오보를 내는 비율 (목표 20% 이하)
