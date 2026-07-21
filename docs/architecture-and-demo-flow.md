# RCA Agent 아키텍처 및 데모 시나리오 흐름

## 1. Fargate Stack (Strands) — 9단계 파이프라인

### 1.1. 전체 플로우

SQS 메시지 수신부터 SNS 알림 발행까지, 9단계 파이프라인의 전체 흐름입니다. 검증 루프 내에서 Beam Selection으로 우선순위 상위 N개(기본 3) 가설만 선택적으로 검증합니다.

```mermaid
flowchart TD
    subgraph Input["입력 & 사전 검증"]
        SQS["SQS Long Polling<br/>(WaitTimeSeconds=20)"]
        PARSE["AlarmPayload 파싱<br/>(SNS envelope unwrap)"]
        DEDUP["멱등성 체크<br/>(DynamoDB IDEMP# 키)"]
        STALE["Stale 알람 체크<br/>(30분 초과 → OUTDATED)"]
        SESSION["세션 생성<br/>(engine: strands)"]
        SQS --> PARSE --> DEDUP --> STALE --> SESSION
    end

    subgraph F1["F1: Scoping"]
        S_RPT["S3 Vectors<br/>유사 보고서 검색"]
        S_AGENT["Scoping Agent<br/>(Execution 티어: Sonnet 4.6 (thinking 없음))<br/>AWS Knowledge + CW + CT MCP"]
        S_OUT["ScopingResult<br/>(severity, blast_radius,<br/>anomaly_start_time, similar_reports)"]
        S_RPT --> S_AGENT --> S_OUT
    end

    subgraph F2["F2: Hypothesis Generation"]
        H_AGENT["Hypothesis Agent<br/>(Planning 티어: Sonnet 4.6)"]
        H_OUT["Hypothesis[] (3~5개, depth=0)<br/>각 가설: description, category,<br/>confidence_score, required_evidence"]
        H_DDB["DynamoDB 가설 저장<br/>(HYPO# 레코드)"]
        H_AGENT --> H_OUT --> H_DDB
    end

    subgraph Loop["검증 루프 (최대 3회)"]
        direction TB
        subgraph F3["F3: Prioritization"]
            P_AGENT["Prioritization Agent<br/>(Planning 티어)"]
            P_OUT["PrioritizedHypothesis[]<br/>(rank, validation_plan)"]
            P_AGENT --> P_OUT
        end

        subgraph BEAM["Beam Selection"]
            B_SEL["PENDING/NEEDS_INVESTIGATION만 필터<br/>→ 상위 N개 선택<br/>(RCA_BEAM_WIDTH=3)"]
        end

        subgraph F4["F4: Evidence Collection"]
            E_AGENT["Evidence Agent<br/>(Execution 티어: Sonnet 4.6 (thinking 없음))<br/>가설별 독립 Agent 인스턴스"]
            E_MCP["AWS Knowledge + CW + CT + GitHub MCP<br/>· 메트릭/로그 수집<br/>· 배포/변경 이력<br/>· 코드 diff 분석"]
            E_PARENT["부모 가설 요약 주입<br/>(depth > 0인 경우)"]
            E_SAVE["DDB: evidence_summary<br/>S3: full evidence 저장"]
            E_MAP["evidence_map<br/>(hypothesis_id → 요약 텍스트)"]
            E_AGENT --> E_MCP
            E_PARENT -.-> E_AGENT
            E_AGENT --> E_SAVE
            E_AGENT --> E_MAP
        end

        subgraph F5["F5: Validation"]
            V_AGENT["Validation Agent<br/>(Execution 티어: Sonnet 4.6 (thinking 없음))<br/>ThreadPoolExecutor 병렬 실행"]
            V_CLASSIFY["신뢰도 기반 재분류<br/>≥0.8 → CONFIRMED<br/>≤0.3 → REJECTED<br/>그 외 → NEEDS_INVESTIGATION"]
            V_GUARD["증거 수집 실패 시<br/>CONFIRMED 방지 가드레일"]
            V_OUT["ValidationJudgment[]<br/>+ all_rejected 플래그"]
            V_AGENT --> V_CLASSIFY --> V_GUARD --> V_OUT
        end

        subgraph TC["Termination Check"]
            T_CHECK["순수 로직 (LLM 미사용)<br/>4가지 종료 조건 OR 평가"]
            T_CONDS["1. confidence ≥ 0.9 (CONFIRMED)<br/>2. 시간 ≥ 20분 (TIME_BUDGET)<br/>3. tree depth > 5 (MAX_DEPTH)<br/>4. 검증 루프 > 3회 (MAX_LOOPS)"]
            T_DEC{{"종료?"}}
            T_CHECK --> T_CONDS --> T_DEC
        end

        subgraph F6["F6: Branching"]
            B_AGENT["Branching Agent<br/>(Planning 티어: Sonnet 4.6)"]
            B_DEDUP["중복 제거<br/>(부모/기각 가설과 비교)"]
            B_OUT["Child Hypothesis[]<br/>(depth = parent+1, max_depth=3)"]
            B_AGENT --> B_DEDUP --> B_OUT
        end

        F3 --> BEAM --> F4 --> F5 --> TC
        T_DEC -->|계속| F6
        F6 -->|새 하위 가설 추가| F3
    end

    subgraph Regen["가설 재생성"]
        REGEN_CHECK{"재생성 횟수<br/>≤ 2회?"}
        REGEN_DO["기존 가설 전체 REJECTED 처리<br/>→ Hypothesis Agent 재호출<br/>→ 새 가설로 루프 재개"]
        REGEN_FAIL["최대 재생성 초과<br/>→ 루프 종료"]
        REGEN_CHECK -->|Yes| REGEN_DO
        REGEN_CHECK -->|No| REGEN_FAIL
    end

    subgraph Finalize["마무리"]
        CLOSE["미해결 가설 최종 분류<br/>CONFIRMED 종료 → REJECTED<br/>기타 종료: 저신뢰도 → REJECTED,<br/>나머지 → CLOSED"]

        subgraph F7["F7: Report"]
            R_AGENT["Report Agent<br/>(Planning 티어: Sonnet 4.6)"]
            R_S3["S3 보고서 저장<br/>(reports/{rca_id}.md)"]
            R_AGENT --> R_S3
        end

        subgraph F8["F8: Playbook"]
            PB_SEARCH["S3 Vectors<br/>기존 플레이북 검색 (≥0.86)"]
            PB_AGENT["Playbook Agent<br/>(Planning 티어: Sonnet 4.6)<br/>update or create"]
            PB_S3V["S3 Vectors 인덱싱"]
            PB_SEARCH --> PB_AGENT --> PB_S3V
        end

        subgraph F9["F9: Notification"]
            N_BUILD["build_notification()<br/>(플레이북 포함)"]
            N_SNS["SNS Publish<br/>(presigned URL + 플레이북)"]
            N_BUILD --> N_SNS
        end

        CLOSE --> F7 --> F8 --> F9
    end

    subgraph Complete["세션 완료"]
        MARK["mark_completed()<br/>state → COMPLETED<br/>root_cause, confirmed 저장"]
        DEL_MSG["SQS 메시지 삭제"]
        MARK --> DEL_MSG
    end

    SESSION --> F1
    S_OUT --> F2
    H_DDB --> Loop
    T_DEC -->|종료| Finalize
    V_OUT -->|all_rejected| Regen
    REGEN_DO --> Loop
    REGEN_FAIL --> Finalize
    F9 --> Complete

    style F4 fill:#e8f5e9,stroke:#388e3c
    style TC fill:#f9f3e3,stroke:#d4a843
    style BEAM fill:#e8eaf6,stroke:#3f51b5
    style Regen fill:#fce4ec,stroke:#c62828
    style Finalize fill:#f3e5f5,stroke:#7b1fa2
```

### 1.2. 상태 전이 다이어그램

DynamoDB에 기록되는 RCA 세션 상태 전이입니다.

```mermaid
stateDiagram-v2
    [*] --> ALARM_RECEIVED: SQS 메시지 수신 + 세션 생성

    ALARM_RECEIVED --> OUTDATED: Stale 알람 (30분 초과)
    ALARM_RECEIVED --> SCOPING: AlarmPayload 파싱 완료

    SCOPING --> HYPOTHESIS_GENERATION: ScopingResult 생성

    HYPOTHESIS_GENERATION --> HYPOTHESIS_PRIORITIZATION: 가설 3~5개 생성

    HYPOTHESIS_PRIORITIZATION --> EVIDENCE_COLLECTION: Beam Selection 후 검증 순서 결정

    EVIDENCE_COLLECTION --> HYPOTHESIS_VALIDATION: evidence_map 구성
    note right of EVIDENCE_COLLECTION
        가설별 독립 Agent 인스턴스
        AWS Knowledge + CW + CT + GitHub MCP
        부모 가설 요약 주입 (depth > 0)
        DDB + S3에 증거 직접 저장
    end note

    HYPOTHESIS_VALIDATION --> REPORT_GENERATION: 종료 조건 충족 또는 분기 불가
    HYPOTHESIS_VALIDATION --> HYPOTHESIS_PRIORITIZATION: 분기 후 재루프
    HYPOTHESIS_VALIDATION --> EVIDENCE_COLLECTION: 재검증 필요
    HYPOTHESIS_VALIDATION --> HYPOTHESIS_GENERATION: 전체 기각 (재생성, 최대 2회)

    REPORT_GENERATION --> COMPLETED: 보고서 + 플레이북 + SNS 알림

    state FAILED_STATE <<join>>
    SCOPING --> FAILED_STATE: 예외 발생
    HYPOTHESIS_GENERATION --> FAILED_STATE: 가설 없음
    EVIDENCE_COLLECTION --> FAILED_STATE: 예외 발생
    HYPOTHESIS_VALIDATION --> FAILED_STATE: 예외 발생
    REPORT_GENERATION --> FAILED_STATE: 예외 발생
    FAILED_STATE --> FAILED

    SCOPING --> CANCELLED: 외부 취소 요청
    HYPOTHESIS_GENERATION --> CANCELLED: 외부 취소 요청
    EVIDENCE_COLLECTION --> CANCELLED: 외부 취소 요청
    HYPOTHESIS_VALIDATION --> CANCELLED: 외부 취소 요청

    COMPLETED --> [*]
    FAILED --> [*]
    OUTDATED --> [*]
    CANCELLED --> [*]
```

### 1.3. 모델 티어 매핑

```mermaid
flowchart LR
    subgraph Planning["Planning 티어<br/>(Sonnet 4.6 + Adaptive Thinking)"]
        HYP["F2: Hypothesis Gen"]
        PRIO["F3: Prioritization"]
        BRANCH["F6: Branching"]
        REPORT["F7: Report"]
        PLAYBOOK["F8: Playbook"]
    end

    subgraph Execution["Execution 티어<br/>(Sonnet 4.6, thinking 없음)"]
        SCOPING["F1: Scoping"]
        EVIDENCE["F4: Evidence Collection"]
        VALIDATION["F5: Validation"]
    end

    subgraph NoLLM["순수 로직 (LLM 미사용)"]
        TERM["Termination Check"]
        NOTIF["F9: Notification"]
    end

    subgraph MCP["MCP 서버 연결"]
        AK["AWS Knowledge MCP"]
        CW["CloudWatch MCP"]
        CT["CloudTrail MCP"]
        GH["GitHub MCP"]
    end

    SCOPING -.-> AK & CW & CT
    EVIDENCE -.-> AK & CW & CT & GH

    style Planning fill:#e3f2fd,stroke:#1565c0
    style Execution fill:#e8f5e9,stroke:#2e7d32
    style NoLLM fill:#f5f5f5,stroke:#616161
    style MCP fill:#fff3e0,stroke:#ef6c00
```

### 1.4. 단계별 데이터 흐름

| 단계 | 입력 | 출력 | 모델 티어 | MCP 도구 |
|------|------|------|----------|---------|
| F1: Scoping | AlarmPayload | ScopingResult (severity, blast_radius, similar_reports, anomaly_start_time) | Execution | AWS Knowledge + CW + CT |
| F2: Hypothesis Gen | ScopingResult | Hypothesis[] (3~5개, depth=0) | Planning | - |
| F3: Prioritization | Hypothesis[] + ScopingResult | PrioritizedHypothesis[] (rank, plan) | Planning | - |
| Beam Selection | PrioritizedHypothesis[] | 상위 N개 필터 (기본 3) | 순수 로직 | - |
| F4: Evidence | Beam 가설 + ScopingResult | evidence_map (hypothesis_id → 요약) | Execution | AWS Knowledge + CW + CT + GH |
| F5: Validation | Beam 가설 + evidence_map | ValidationJudgment[] + all_rejected | Execution | - |
| Termination | judgments + hypotheses + start_time | TerminationDecision (should_terminate, reason) | 순수 로직 | - |
| F6: Branching | NEEDS_INVESTIGATION 가설 + evidence | Child Hypothesis[] (depth+1) | Planning | - |
| F7: Report | best_hypothesis + evidence + timeline | RcaReport (Markdown) → S3 저장 + S3 Vectors 인덱싱 | Planning | - |
| F8: Playbook | RcaReport | Playbook → S3 Vectors 인덱싱 | Planning | - |
| F9: Notification | RcaReport + Playbook | SNS 메시지 (presigned URL + 플레이북) | 순수 로직 | - |

### 1.5. 주요 설정값

| 상수 | 기본값 | 용도 |
|------|--------|------|
| `RCA_BEAM_WIDTH` | 3 | 루프당 검증할 가설 수 |
| `RCA_MAX_VALIDATION_LOOPS` | 3 | 검증 루프 최대 반복 |
| `RCA_MAX_REGENERATION_ROUNDS` | 2 | 전체 기각 시 재생성 최대 횟수 |
| `RCA_TIME_BUDGET_SECONDS` | 1200 | 시간 예산 (20분) |
| `RCA_MAX_TREE_DEPTH` | 5 | 가설 트리 최대 깊이 |
| `TERMINATION_CONFIDENCE_THRESHOLD` | 0.9 | 종료 판단 신뢰도 임계치 |
| `CONFIRMATION_THRESHOLD` | 0.8 | CONFIRMED 분류 임계치 |
| `REJECTION_THRESHOLD` | 0.3 | REJECTED 분류 임계치 |
| `MAX_BRANCHING_DEPTH` | 3 | 분기 최대 깊이 |
| `ALARM_STALENESS_SECONDS` | 1800 | Stale 알람 판정 (30분) |

---

## 2. Fargate Stack (CC Headless) — 전문 서브 에이전트 오케스트레이션

### 2.1. 전체 플로우

Python 핸들러가 SQS 수신과 claim 기반 세션 소유권을 관리하고, CC Headless 메인
에이전트가 RCA, 조건부 Remediation, Report 전문 서브 에이전트를 순차 호출합니다.
Artifact Watcher는 실행 토큰별 격리 디렉터리를 감시하지만 현재 claim과 일치할
때만 DynamoDB trace를 기록합니다.

```mermaid
flowchart TD
    subgraph Input["입력 & 사전 검증"]
        SQS["SQS Long Polling<br/>(WaitTimeSeconds=20)"]
        PARSE["AlarmPayload 파싱<br/>(SNS envelope unwrap)"]
        DEDUP["세션 claim<br/>(receive count + claim token)"]
        STALE["Stale 알람 체크<br/>(30분 초과 → OUTDATED)"]
        SESSION["세션 생성<br/>(engine: cc-headless)"]
        SQS --> PARSE --> DEDUP --> STALE --> SESSION
    end

    subgraph Prepare["실행 준비"]
        ARTIFACT_DIR["실행 토큰별 산출물 디렉터리 생성"]
        ALARM_PARSE["AlarmContext 구성<br/>(alarm_name, region, metric,<br/>dimensions, threshold)"]
        PROMPT["프롬프트 조립<br/>system (rca-system.md)<br/>+ user (rca-user.md + 알람 데이터)"]
        WATCHER["Artifact Watcher 스레드 시작<br/>(3초 간격 폴링)"]
        ARTIFACT_DIR --> ALARM_PARSE --> PROMPT --> WATCHER
    end

    subgraph CCExec["CC CLI Subprocess"]
        CC_CMD["claude -p {prompt}<br/>--output-format json<br/>--dangerously-skip-permissions<br/>--mcp-config mcp-config.json"]
        CC_CANCEL["Cancel Checker 스레드<br/>(15초 간격 DDB 상태 확인)"]
        CC_CMD -.-> CC_CANCEL
    end

    subgraph MCPTools["MCP 도구 (CC 자율 호출)"]
        AK["AWS Knowledge MCP<br/>AWS 문서 참조"]
        CW["CloudWatch MCP<br/>메트릭/로그 수집"]
        CT["CloudTrail MCP<br/>배포/변경 이력"]
        GH["GitHub MCP<br/>코드 변경 분석"]
        PROGRESS["rca-progress MCP<br/>산출물 저장 + 제한된 reset"]
    end

    subgraph RCA["전문 서브 에이전트"]
        direction TB
        STEP1["1. RCA specialist<br/>스코핑 → 가설 → 증거 → 검증"]
        GATE{{"확정 원인?"}}
        STEP2["2. Remediation specialist<br/>허용된 reset + 서버 사후 검증"]
        STEP3["3. Report specialist<br/>report.md + playbook.json"]
        STEP1 --> GATE
        GATE -->|Yes| STEP2 --> STEP3
        GATE -->|No| STEP3
    end

    subgraph WatcherDetail["Artifact Watcher (백그라운드)"]
        W_DETECT["파일 감지<br/>(RCA, remediation, report,<br/>playbook 산출물)"]
        W_SPAN["claim 조건부 DDB SPAN 기록"]
        W_HYPO["DDB HYPO 레코드<br/>생성/갱신"]
        W_DETECT --> W_SPAN
        W_DETECT --> W_HYPO
    end

    subgraph Output["결과 처리"]
        REPORT_PARSE["report.md 읽기<br/>+ 근본원인 추출 (regex)"]
        CONTRACT["서버 소유 remediation 결과와<br/>보고서·플레이북 교차 검증"]
        S3_REPORT["시도 격리 S3 보고서 저장"]
        PB_PARSE["playbook.json 파싱<br/>→ S3 Vectors 인덱싱"]
        SNS_NOTIFY["SNS 알림 발행<br/>(presigned URL + 플레이북)"]
        DDB_COMPLETE["DDB 세션 갱신<br/>(state → COMPLETED,<br/>root_cause 저장)"]
        DEL_MSG["SQS 메시지 삭제"]
        REPORT_PARSE --> CONTRACT --> S3_REPORT --> PB_PARSE --> SNS_NOTIFY --> DDB_COMPLETE --> DEL_MSG
    end

    SESSION --> Prepare
    WATCHER --> CCExec
    CC_CMD <--> MCPTools
    CC_CMD <--> RCA
    RCA -.->|파일 생성| WatcherDetail
    CC_CMD --> Output

    style CCExec fill:#e3f2fd,stroke:#1565c0
    style RCA fill:#f3e5f5,stroke:#7b1fa2
    style MCPTools fill:#fff3e0,stroke:#ef6c00
    style WatcherDetail fill:#e0f2f1,stroke:#00695c
```

### 2.2. 상태 전이 다이어그램

CC Headless는 두 개의 활성 세션 상태를 유지하고 세부 단계는 claim 조건부
SPAN/HYPO 레코드로 기록합니다. 완료 세션 중복만 ACK하며 claim 경합이나 소유권
확인 실패는 SQS 재전달 대상으로 남깁니다.

```mermaid
stateDiagram-v2
    [*] --> ALARM_RECEIVED: SQS Long Polling + 세션 생성

    ALARM_RECEIVED --> [*]: 완료 세션 중복 → ACK
    ALARM_RECEIVED --> [*]: claim 경합/조회 실패 → 재전달
    ALARM_RECEIVED --> ANALYZING: 멱등성 체크 통과
    ALARM_RECEIVED --> OUTDATED: Stale 알람 (30분 초과)

    ANALYZING --> COMPLETED: CC CLI 성공<br/>보고서 S3 저장 + SNS 알림
    ANALYZING --> FAILED: CC 오류 / 타임아웃 (30분)
    ANALYZING --> CANCELLED: 외부 취소 요청 감지<br/>(15초 간격 DDB 폴링)

    OUTDATED --> [*]

    note right of ANALYZING
        CC CLI subprocess 실행 중
        Artifact Watcher가 /tmp 감시
        산출물 파일 → DDB SPAN/HYPO 기록:
        · scoping.json → SCOPING 스팬
        · hypotheses.json → HYPO 레코드
        · validation-N.json → VALIDATION_LOOP 스팬 + 가설 갱신
        · remediation.json → REMEDIATION 스팬
        · report.md → REPORT 스팬
        · playbook.json → PLAYBOOK 스팬
    end note

    COMPLETED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
```

### 2.3. 전문 서브 에이전트 워크플로우

CC 메인 에이전트는 오케스트레이션만 담당하고 모든 도메인 작업을 역할별 전문
서브 에이전트에 위임합니다.

```mermaid
flowchart TD
    ORCH["Headless 오케스트레이터"]
    RCA["RCA specialist<br/>읽기 전용 도구<br/>스코핑·가설·검증 산출물"]
    CONFIRMED{{"확정 원인 존재?"}}
    REM["Remediation specialist<br/>서버 검증형 reset<br/>CloudWatch 사후 판정"]
    REPORT["Report specialist<br/>서버 결과 기반 보고서·플레이북"]
    CHECK["완료 게이트<br/>산출물 스키마·상태 교차 검증"]

    ORCH --> RCA --> CONFIRMED
    CONFIRMED -->|Yes| REM --> REPORT
    CONFIRMED -->|No| REPORT
    REPORT --> CHECK

    style ORCH fill:#f5f5f5,stroke:#616161
    style RCA fill:#e3f2fd,stroke:#1565c0
    style REM fill:#ffebee,stroke:#c62828
    style REPORT fill:#e8f5e9,stroke:#2e7d32
```

### 2.4. MCP 서버 구성

| MCP 서버 | 실행 방식 | 용도 |
|---------|----------|------|
| `aws-knowledge` | `fastmcp run https://knowledge-mcp.global.api.aws` | AWS 서비스 문서 참조, 장애 패턴 검색 |
| `cloudwatch` | `uvx awslabs.cloudwatch-mcp-server` | 메트릭 조회, Logs Insights 쿼리, 알람 조회 |
| `cloudtrail` | `uvx awslabs.cloudtrail-mcp-server` | 배포/변경 이벤트 조회, Lake SQL 분석 |
| `github` | `github-mcp-server stdio` | 커밋 diff, PR diff, 파일 내용 조회 |
| `rca-progress` | `fastmcp run` | 격리 산출물 저장과 서버 검증형 Healthcare reset |

### 2.5. Artifact Watcher 파일 → DDB 매핑

| 파일 | DDB 스팬 타입 | 추가 동작 |
|------|-------------|----------|
| `scoping.json` | `SCOPING` | — |
| `hypotheses.json` | `HYPOTHESIS_GENERATION` | HYPO# 레코드 batch write (최대 25개) |
| `validation-N.json` | `VALIDATION_LOOP` | HYPO# 레코드 상태 갱신 (confirmed/rejected/closed/needs_investigation) |
| `remediation.json` | `REMEDIATION` | reset 결과와 CloudWatch 사후 검증 상태 |
| `report.md` | `REPORT` | — |
| `playbook.json` | `PLAYBOOK` | failure_type, tags 등 메타데이터 저장 |

---

## 3. 두 스택 비교

| | Fargate Stack (Strands) | Fargate Stack (CC Headless) |
|---|---|---|
| **실행 환경** | ECS Fargate (Long Polling) | ECS Fargate (Long Polling) |
| **에이전트 엔진** | Strands Agents SDK (Python) | Claude Code CLI (headless, Bedrock) |
| **RCA 방식** | 9단계 코드 기반 파이프라인 | RCA·Remediation·Report 전문 에이전트 오케스트레이션 |
| **모델** | 단일 Sonnet 4.6 + Planning/Execution 행동 분리 (adaptive thinking 유무) | CC 기본 모델 (Sonnet 4.6) |
| **서브에이전트** | Strands Agent 인스턴스 (코드로 생성) | CC Agent tool (프롬프트로 스폰) |
| **상태 관리** | Python 코드가 매 단계 DDB 업데이트 | Artifact Watcher가 파일 감시 → DDB 기록 |
| **DDB 상태 수** | 7개 활성 상태 + 4개 terminal | 2개 활성 상태 + 4개 terminal |
| **자동 복구** | 별도 워커, 기본 비활성 | 확정 원인의 허용된 Healthcare reset만 실행 |
| **타임아웃** | 20분 (RCA_TIME_BUDGET_SECONDS) | 30분 (CC_TIMEOUT_SECONDS) |
| **취소 감지** | update_state() 시 ConditionExpression | Cancel Checker 스레드 (15초 간격 DDB 폴링) |
| **증거 격리** | 가설별 독립 Agent 인스턴스 | CC 자체 컨텍스트 관리 |
| **공유 리소스** | SNS (알람/알림), DynamoDB, S3, S3 Vectors |

---

## 4. 데모 시나리오: DB 커넥션 누수 장애

### 시나리오 개요

최근 배포된 코드가 DB 커넥션을 세션마다 열기만 하고 닫지 않아 커넥션이 누적됩니다. RDS DatabaseConnections가 한계에 도달하면서 서비스 전체에 장애가 전파됩니다.

### 데모 흐름 (Strands Agent)

```mermaid
sequenceDiagram
    participant CW as CloudWatch Alarm
    participant SQS as SQS Queue
    participant Agent as RCA Agent<br/>(ECS Fargate)
    participant AK_MCP as AWS Knowledge MCP
    participant CW_MCP as CloudWatch MCP
    participant CT_MCP as CloudTrail MCP
    participant GH_MCP as GitHub MCP
    participant Bedrock as Amazon Bedrock
    participant S3V as S3 Vectors
    participant S3 as S3
    participant DDB as DynamoDB
    participant SNS as SNS → SRE

    Note over CW,SQS: Phase 0: 알람 수신
    CW->>SQS: RDS DatabaseConnections 임계치 초과
    SQS->>Agent: Long Polling으로 수신
    Agent->>DDB: 세션 생성 (ALARM_RECEIVED)
    Agent->>DDB: 멱등성 체크 통과

    Note over Agent,CW_MCP: Phase 1: F1 초기 스코핑
    Agent->>DDB: state = SCOPING
    Agent->>S3V: 유사 보고서 검색
    S3V-->>Agent: (유사 보고서 없음)
    Agent->>AK_MCP: RDS 장애 패턴 / 트러블슈팅 가이드 검색
    Agent->>CW_MCP: DB 커넥션 수 추이 조회 (30분)
    CW_MCP-->>Agent: 커넥션 수 선형 증가 확인
    Agent->>CW_MCP: 서비스 Latency/에러율 조회
    CW_MCP-->>Agent: Latency 급증 + 5xx 에러 증가
    Agent->>Agent: ScopingResult 생성<br/>(severity=high, blast=multi)

    Note over Agent,Bedrock: Phase 2: F2 가설 생성
    Agent->>DDB: state = HYPOTHESIS_GENERATION
    Agent->>Bedrock: 스코핑 결과 기반 가설 요청 (Sonnet 4.6)
    Bedrock-->>Agent: 3개 가설 반환
    Agent->>DDB: HYPO# 레코드 저장
    Note right of Agent: A: 최근 배포 코드 결함 (0.7)<br/>B: 트래픽 급증 (0.5)<br/>C: RDS 인스턴스 문제 (0.4)

    Note over Agent,Bedrock: Loop 1: F3 우선순위 + Beam Selection
    Agent->>DDB: state = HYPOTHESIS_PRIORITIZATION
    Agent->>Bedrock: 가설 우선순위 요청 (Sonnet 4.6)
    Bedrock-->>Agent: A → B → C 순서
    Agent->>Agent: Beam Selection: 3개 전부 선택

    Note over Agent,CT_MCP: Loop 1: F4 증거 수집 (가설별 독립 Agent)
    Agent->>DDB: state = EVIDENCE_COLLECTION

    rect rgb(232, 245, 233)
        Note over Agent,CT_MCP: 가설 A 증거 수집 (독립 Agent 인스턴스)
        Agent->>CT_MCP: 최근 배포 이벤트 조회
        CT_MCP-->>Agent: 장애 2시간 전 ECS 배포 확인
        Agent->>CW_MCP: DB 커넥션 메트릭 (배포 전후 비교)
        CW_MCP-->>Agent: 배포 시점부터 커넥션 선형 증가
        Agent->>CW_MCP: 로그 검색 (connection, error)
        CW_MCP-->>Agent: "Too many connections" 에러 다수
        Agent->>S3: full evidence 저장 (rca/{id}/evidence/{hypo_id}/combined.md)
        Agent->>DDB: evidence_summary 저장
    end

    rect rgb(232, 245, 233)
        Note over Agent,CW_MCP: 가설 B 증거 수집 (독립 Agent 인스턴스)
        Agent->>CW_MCP: RequestCount 메트릭 조회
        CW_MCP-->>Agent: 요청 수 평소 수준
        Agent->>S3: full evidence 저장
    end

    rect rgb(232, 245, 233)
        Note over Agent,CW_MCP: 가설 C 증거 수집 (독립 Agent 인스턴스)
        Agent->>CW_MCP: FreeStorageSpace, CPUUtilization 조회
        CW_MCP-->>Agent: 모두 정상 범위
        Agent->>S3: full evidence 저장
    end

    Note over Agent,Bedrock: Loop 1: F5 가설 검증
    Agent->>DDB: state = HYPOTHESIS_VALIDATION
    Agent->>Bedrock: 가설 A + 증거 요약 → 검증 (Execution: Sonnet 4.6)
    Bedrock-->>Agent: A: NEEDS_INVESTIGATION (0.75)<br/>배포 상관관계 높으나 구체적 코드 결함 미확인
    Agent->>Bedrock: 가설 B + 증거 요약 → 검증
    Bedrock-->>Agent: B: REJECTED (0.1)
    Agent->>Bedrock: 가설 C + 증거 요약 → 검증
    Bedrock-->>Agent: C: REJECTED (0.15)
    Agent->>DDB: 가설 상태 갱신

    Note over Agent,Bedrock: Loop 1: 종료 판단 + F6 분기
    Agent->>Agent: 종료 조건 미충족 → 계속
    Agent->>Bedrock: 가설 A 하위 분기 요청 (Sonnet 4.6)
    Bedrock-->>Agent: 하위 가설 생성
    Agent->>DDB: HYPO# 레코드 저장
    Note right of Agent: A-1: 커넥션 풀 설정 변경 (0.4)<br/>A-2: 코드에서 커넥션 미반환 (0.7)

    Note over Agent,CT_MCP: Loop 2: F3→Beam→F4→F5
    Agent->>DDB: state = HYPOTHESIS_PRIORITIZATION
    Agent->>Agent: Beam Selection: A-1, A-2 선택
    Agent->>DDB: state = EVIDENCE_COLLECTION

    rect rgb(232, 245, 233)
        Note over Agent,GH_MCP: A-1, A-2 증거 수집 (각각 독립 Agent)
        Agent->>CT_MCP: 배포 변경 상세 조회
        CT_MCP-->>Agent: RegisterTaskDefinition 이벤트 상세
        Agent->>GH_MCP: 배포 커밋 diff 조회 (get_commit)
        GH_MCP-->>Agent: db.py에서 connection.close() 제거 확인
        Agent->>CW_MCP: 커넥션 추이 상세 분석
        CW_MCP-->>Agent: 배포 시점부터 선형 증가 (풀 설정 변경 아닌 누수 패턴)
        Agent->>S3: full evidence 저장
    end

    Agent->>DDB: state = HYPOTHESIS_VALIDATION
    Agent->>Bedrock: A-1 + 증거 → 검증 (Execution: Sonnet 4.6)
    Bedrock-->>Agent: A-1: REJECTED (0.2)
    Agent->>Bedrock: A-2 + 증거 → 검증 (Execution: Sonnet 4.6)
    Bedrock-->>Agent: A-2: CONFIRMED (0.92)

    Note over Agent,Bedrock: 종료 → confidence ≥ 0.9 (CONFIRMED)

    Note over Agent: CONFIRMED 종료 → 미해결 가설 REJECTED 처리
    Agent->>DDB: A-1 → REJECTED ("확정된 근본원인 발견으로 기각")
    Agent->>DDB: A → REJECTED ("확정된 근본원인 발견으로 기각")

    Note over Agent,S3: F7 보고서 생성
    Agent->>DDB: state = REPORT_GENERATION
    Agent->>Bedrock: RCA 보고서 작성 요청 (Sonnet 4.6)
    Bedrock-->>Agent: 구조화된 보고서
    Agent->>S3: reports/{rca_id}.md 저장

    Note over Agent,S3V: F8 플레이북 생성
    Agent->>S3V: 기존 유사 플레이북 검색 (≥0.86)
    S3V-->>Agent: (해당 없음 → 신규 생성)
    Agent->>Bedrock: 플레이북 생성 요청 (Sonnet 4.6)
    Bedrock-->>Agent: DB 커넥션 누수 플레이북
    Agent->>S3V: 플레이북 임베딩 인덱싱

    Note over Agent,SNS: F9 알림
    Agent->>S3: presigned URL 생성
    Agent->>SNS: RCA 완료 알림 발행 (플레이북 포함)
    Agent->>DDB: state = COMPLETED (root_cause, confirmed=true)
```

### 각 Phase별 산출물

| Phase | 단계 | 주요 산출물 | 저장소 |
|-------|------|-----------|--------|
| 0 | 알람 수신 | AlarmPayload, RCA 세션 | DynamoDB |
| 1 | F1 스코핑 | ScopingResult (severity=high, blast=multi) | - |
| 2 | F2 가설 생성 | 가설 A/B/C (3개) | DynamoDB (HYPO#) |
| 3 | F3 우선순위 + Beam Selection | A→B→C 검증 순서, 상위 3개 선택 | - |
| 4 | F4 증거 수집 | 메트릭(커넥션 추이), 로그(Too many connections), 배포 이력, 코드 diff | S3, DynamoDB |
| 5 | F5 검증 (1차) | A: NEEDS_INVESTIGATION, B/C: REJECTED | DynamoDB |
| 6 | F6 분기 | A-1(풀 설정), A-2(커넥션 미반환) | DynamoDB (HYPO#) |
| 4-5 | F4-F5 (2차) | A-1: REJECTED, A-2: CONFIRMED (0.92) | S3, DynamoDB |
| - | REJECTED 처리 | A-1, A → REJECTED (확정된 근본원인 발견으로 기각) | DynamoDB |
| 7 | F7 보고서 | RCA Report (Markdown) | S3 |
| 8 | F8 플레이북 | DB 커넥션 누수 대응 플레이북 | S3 Vectors |
| 9 | F9 알림 | SNS 알림 (presigned URL + 플레이북 포함) | SNS → SRE |

### 데모에서 사용되는 MCP 도구

| MCP 서버 | 도구 | 용도 |
|---------|------|------|
| AWS Knowledge MCP | `search_documentation`, `read_documentation` | AWS 서비스 문서 참조, 모범 사례 검색 |
| CloudWatch MCP | `get_metric_data` | DB 커넥션 수, Latency, RequestCount, CPU 메트릭 조회 |
| CloudWatch MCP | `execute_log_insights_query` | "Too many connections" 에러 로그 검색 |
| CloudWatch MCP | `analyze_metric` | 커넥션 증가 트렌드 분석 |
| CloudTrail MCP | `lookup_events` | ECS 배포 이벤트(RegisterTaskDefinition) 조회 |
| GitHub MCP | `get_commit`, `list_commits` | 배포 커밋 diff 조회, 결함 패턴 탐지 |
| GitHub MCP | `pull_request_read` | PR diff, 변경 파일 목록, 리뷰 코멘트 조회 |

### 종료 조건 매핑

이 데모에서는 **CONFIRMED** 종료 조건이 트리거됩니다:
- 가설 A-2 "코드에서 커넥션 미반환"이 confidence 0.92로 확정
- 임계치 0.9 이상 → 즉시 종료 → 나머지 가설 REJECTED 처리 → 보고서 → 플레이북 → 알림

---

## 5. DynamoDB 트레이스 스팬 계층

대시보드 트레이스 그래프에 표시되는 스팬 구조입니다. 두 스택 모두 동일한 DynamoDB 테이블에 스팬을 기록하며, `engine` 필드로 구분됩니다.

### Strands 스팬 구조

```
SCOPING
HYPOTHESIS_GENERATION
VALIDATION_LOOP (반복 컨테이너)
  ├─ PRIORITIZATION
  ├─ EVIDENCE_COLLECTION
  ├─ VALIDATION
  ├─ TERMINATION
  ├─ BRANCHING (NEEDS_INVESTIGATION 존재 시)
  └─ HYPOTHESIS_GENERATION (재생성 시)
REPORT
PLAYBOOK
NOTIFICATION
```

### CC Headless 스팬 구조

```
SCOPING (scoping.json 감지 시)
HYPOTHESIS_GENERATION (hypotheses.json 감지 시)
VALIDATION_LOOP (validation-N.json 감지 시, N=1,2,3)
REMEDIATION (remediation.json 감지 시)
REPORT (report.md 감지 시)
PLAYBOOK (playbook.json 감지 시)
```
