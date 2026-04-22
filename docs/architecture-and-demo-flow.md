# RCA Agent 아키텍처 및 데모 시나리오 흐름

## 1. 전체 데이터 플로우 — Fargate (Strands, 10단계)

SQS 메시지 수신부터 SNS 알림 발행까지, 10단계 파이프라인의 전체 데이터 흐름을 나타냅니다.

```mermaid
flowchart TD
    subgraph Input["입력"]
        SQS["SQS Message"]
        PARSE["AlarmPayload 파싱"]
        DEDUP["멱등성 체크<br/>(DynamoDB)"]
        SQS --> PARSE --> DEDUP
    end

    subgraph F1["F1: Scoping"]
        S_PB["S3 Vectors<br/>유사 플레이북 검색"]
        S_AGENT["Scoping Agent<br/>(CW+CT MCP)"]
        S_OUT["ScopingResult"]
        S_PB --> S_AGENT --> S_OUT
    end

    subgraph F2["F2: Hypothesis Generation"]
        H_AGENT["Hypothesis Agent<br/>(Planning Model)"]
        H_OUT["Hypothesis[] (3~5개)"]
        H_AGENT --> H_OUT
    end

    subgraph Loop["검증 루프 (최대 3회)"]
        direction TB
        subgraph F3["F3: Prioritization"]
            P_AGENT["Prioritization Agent"]
            P_OUT["PrioritizedHypothesis[]"]
            P_AGENT --> P_OUT
        end

        subgraph F4["F4: Evidence Collection"]
            E_AGENT["Evidence Agent<br/>(CW+CT+GitHub MCP)"]
            E_CW["CloudWatch<br/>메트릭/로그"]
            E_CT["CloudTrail<br/>배포/변경 이력"]
            E_GH["GitHub<br/>코드 변경 분석"]
            E_S3["S3 증거 아카이브"]
            E_MAP["evidence_map"]
            E_AGENT --> E_CW
            E_AGENT --> E_CT
            E_AGENT --> E_GH
            E_AGENT --> E_MAP
            E_MAP --> E_S3
        end

        subgraph F5["F5: Validation"]
            V_AGENT["Validation Agent"]
            V_OUT["ValidationJudgment[]<br/>CONFIRMED / REJECTED /<br/>NEEDS_INVESTIGATION"]
            V_AGENT --> V_OUT
        end

        subgraph F7["F7: Termination Check"]
            T_CHECK["5가지 종료 조건 평가"]
            T_DEC{{"종료?"}}
            T_CHECK --> T_DEC
        end

        subgraph F6["F6: Branching"]
            B_AGENT["Branching Agent"]
            B_OUT["Child Hypothesis[]"]
            B_AGENT --> B_OUT
        end

        F3 --> F4 --> F5 --> F7
        T_DEC -->|계속| F6
        F6 -->|새 하위 가설| F3
    end

    subgraph Regen["가설 재생성"]
        REGEN["전체 기각 시<br/>Hypothesis Agent 재호출<br/>(최대 2회)"]
    end

    subgraph Output["출력"]
        subgraph F8["F8: Report"]
            R_AGENT["Report Agent"]
            R_S3["S3 보고서 저장<br/>(Markdown)"]
            R_AGENT --> R_S3
        end

        subgraph F9["F9: Playbook"]
            PB_SEARCH["S3 Vectors<br/>기존 플레이북 검색"]
            PB_AGENT["Playbook Agent<br/>(update or create)"]
            PB_S3V["S3 Vectors 인덱싱"]
            PB_SEARCH --> PB_AGENT --> PB_S3V
        end

        subgraph F10["F10: Notification"]
            N_SNS["SNS Publish<br/>(presigned URL)"]
        end

        F8 --> F9 --> F10
    end

    DEDUP --> F1
    S_OUT --> F2
    H_OUT --> Loop
    T_DEC -->|종료| F8
    V_OUT -->|all_rejected| Regen
    Regen --> Loop

    style F4 fill:#e8f5e9,stroke:#388e3c
    style F7 fill:#f9f3e3,stroke:#d4a843
    style Regen fill:#fce4ec,stroke:#c62828
```

## 2. 전체 데이터 플로우 — Lambda (CC Headless, 프롬프트 주도)

CC on Bedrock headless 모드에서 단일 프롬프트로 전체 RCA를 수행합니다. CC가 MCP 도구를 자율적으로 호출하며, 동일한 DynamoDB/S3/SNS를 공유합니다.

```mermaid
flowchart TD
    subgraph Input["입력"]
        SQS["SQS Event Source<br/>(batchSize=1)"]
        PARSE["AlarmPayload 파싱"]
        DEDUP["멱등성 체크<br/>(DynamoDB IDEMP# 키)"]
        SESSION["세션 생성<br/>(engine: cc-headless)"]
        SQS --> PARSE --> DEDUP --> SESSION
    end

    subgraph CCExec["CC CLI 실행"]
        PROMPT["프롬프트 조립<br/>(system + user)"]
        CC["Claude Code CLI<br/>--output-format json<br/>--mcp-config mcp-config.json"]
        PROMPT --> CC
    end

    subgraph MCPTools["MCP 도구 (CC 자율 호출)"]
        CW["CloudWatch MCP<br/>메트릭/로그 수집"]
        CT["CloudTrail MCP<br/>배포/변경 이력"]
        GH["GitHub MCP<br/>코드 변경 분석"]
    end

    subgraph RCA["프롬프트 내 RCA 워크플로우"]
        direction TB
        STEP1["Step 1: 초기 스코핑 (2분)"]
        STEP2["Step 2: 가설 생성 (2분)"]
        STEP3["Step 3: 증거 수집 + 검증 루프 (4분)"]
        STEP4["Step 4: 종료 판단 (1분)"]
        STEP5["Step 5: 보고서 생성 (1분)"]
        STEP1 --> STEP2 --> STEP3 --> STEP4 --> STEP5
    end

    subgraph Output["출력"]
        S3_REPORT["S3 보고서 저장<br/>(Markdown)"]
        SNS_NOTIFY["SNS 알림 발행<br/>(presigned URL)"]
        DDB_COMPLETE["DynamoDB 상태 갱신<br/>(COMPLETED)"]
        S3_REPORT --> SNS_NOTIFY --> DDB_COMPLETE
    end

    SESSION --> CCExec
    CC <--> MCPTools
    CC <--> RCA
    CC --> Output

    style CCExec fill:#e3f2fd,stroke:#1565c0
    style RCA fill:#f3e5f5,stroke:#7b1fa2
    style MCPTools fill:#fff3e0,stroke:#ef6c00
```

## 3. 상태 전이 다이어그램

DynamoDB에 기록되는 RCA 세션 상태 전이입니다. 두 스택이 동일한 DynamoDB 테이블을 사용하며, `engine` 필드로 구분합니다.

### Fargate Stack (Strands) 상태 전이

```mermaid
stateDiagram-v2
    [*] --> ALARM_RECEIVED: SQS 메시지 수신

    ALARM_RECEIVED --> SCOPING: AlarmPayload 파싱 완료

    SCOPING --> HYPOTHESIS_GENERATION: ScopingResult 생성

    HYPOTHESIS_GENERATION --> HYPOTHESIS_PRIORITIZATION: 가설 3~5개 생성

    HYPOTHESIS_PRIORITIZATION --> EVIDENCE_COLLECTION: 검증 순서 결정

    EVIDENCE_COLLECTION --> HYPOTHESIS_VALIDATION: evidence_map 구성
    note right of EVIDENCE_COLLECTION
        CloudWatch MCP: 메트릭 + 로그
        CloudTrail MCP: 배포/변경 이력
        GitHub MCP: 코드 변경 diff 분석
        S3에 증거 아카이브
    end note

    HYPOTHESIS_VALIDATION --> REPORT_GENERATION: 종료 조건 충족
    HYPOTHESIS_VALIDATION --> HYPOTHESIS_PRIORITIZATION: 분기 후 재루프
    HYPOTHESIS_VALIDATION --> HYPOTHESIS_GENERATION: 전체 기각 (재생성)

    REPORT_GENERATION --> COMPLETED: 보고서 + 플레이북 + 알림

    state FAILED_STATE <<join>>
    SCOPING --> FAILED_STATE: 예외 발생
    HYPOTHESIS_GENERATION --> FAILED_STATE: 가설 없음
    EVIDENCE_COLLECTION --> FAILED_STATE: 예외 발생
    HYPOTHESIS_VALIDATION --> FAILED_STATE: 예외 발생
    REPORT_GENERATION --> FAILED_STATE: 예외 발생
    FAILED_STATE --> FAILED

    COMPLETED --> [*]
    FAILED --> [*]
```

### Lambda Stack (CC Headless) 상태 전이

```mermaid
stateDiagram-v2
    [*] --> ALARM_RECEIVED: SQS Event Source

    ALARM_RECEIVED --> ANALYZING: 멱등성 체크 통과 + 세션 생성
    ALARM_RECEIVED --> [*]: 중복 감지 → 즉시 반환

    ANALYZING --> COMPLETED: CC CLI 성공<br/>보고서 S3 저장 + SNS 알림
    ANALYZING --> FAILED: CC 오류 / 타임아웃

    note right of ANALYZING
        CC CLI subprocess 실행
        프롬프트 내 5단계 워크플로우 자율 수행
        MCP 도구 자동 호출
        engine: 'cc-headless'
    end note

    COMPLETED --> [*]
    FAILED --> [*]
```

## 4. 데모 시나리오: DB 커넥션 누수 장애

PRD Section 3에 정의된 데모 시나리오의 10단계 파이프라인 흐름입니다.

### 시나리오 개요

최근 배포된 코드가 DB 커넥션을 세션마다 열기만 하고 닫지 않아 커넥션이 누적됩니다. RDS DatabaseConnections가 한계에 도달하면서 서비스 전체에 장애가 전파됩니다.

### 흐름도

```mermaid
sequenceDiagram
    participant CW as CloudWatch Alarm
    participant SQS as SQS Queue
    participant Agent as RCA Agent<br/>(ECS Fargate)
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
    Agent->>S3V: 유사 플레이북 검색
    S3V-->>Agent: (유사 플레이북 없음)
    Agent->>CW_MCP: DB 커넥션 수 추이 조회 (30분)
    CW_MCP-->>Agent: 커넥션 수 선형 증가 확인
    Agent->>CW_MCP: 서비스 Latency/에러율 조회
    CW_MCP-->>Agent: Latency 급증 + 5xx 에러 증가
    Agent->>Agent: ScopingResult 생성<br/>(severity=high, blast=multi)

    Note over Agent,Bedrock: Phase 2: F2 가설 생성
    Agent->>DDB: state = HYPOTHESIS_GENERATION
    Agent->>Bedrock: 스코핑 결과 기반 가설 요청
    Bedrock-->>Agent: 3개 가설 반환
    Note right of Agent: A: 최근 배포 코드 결함 (0.7)<br/>B: 트래픽 급증 (0.5)<br/>C: RDS 인스턴스 문제 (0.4)

    Note over Agent,Bedrock: Phase 3: F3 우선순위 결정
    Agent->>DDB: state = HYPOTHESIS_PRIORITIZATION
    Agent->>Bedrock: 가설 우선순위 요청
    Bedrock-->>Agent: A → B → C 순서

    Note over Agent,CT_MCP: Phase 4: F4 증거 수집
    Agent->>DDB: state = EVIDENCE_COLLECTION

    rect rgb(232, 245, 233)
        Note over Agent,CT_MCP: 가설 A 증거 수집
        Agent->>CT_MCP: 최근 배포 이벤트 조회
        CT_MCP-->>Agent: 장애 2시간 전 ECS 배포 확인
        Agent->>CW_MCP: DB 커넥션 메트릭 (배포 전후 비교)
        CW_MCP-->>Agent: 배포 시점부터 커넥션 선형 증가
        Agent->>CW_MCP: 로그 검색 (connection, error)
        CW_MCP-->>Agent: "Too many connections" 에러 다수
        Agent->>S3: 증거 아카이브 저장
    end

    rect rgb(232, 245, 233)
        Note over Agent,CW_MCP: 가설 B 증거 수집
        Agent->>CW_MCP: RequestCount 메트릭 조회
        CW_MCP-->>Agent: 요청 수 평소 수준
        Agent->>S3: 증거 아카이브 저장
    end

    rect rgb(232, 245, 233)
        Note over Agent,CW_MCP: 가설 C 증거 수집
        Agent->>CW_MCP: FreeStorageSpace, CPUUtilization 조회
        CW_MCP-->>Agent: 모두 정상 범위
        Agent->>S3: 증거 아카이브 저장
    end

    Note over Agent,Bedrock: Phase 5: F5 가설 검증
    Agent->>DDB: state = HYPOTHESIS_VALIDATION
    Agent->>Bedrock: 가설 A + 증거 → 검증
    Bedrock-->>Agent: A: NEEDS_INVESTIGATION (0.75)<br/>배포 상관관계 높으나 구체적 코드 결함 미확인
    Agent->>Bedrock: 가설 B + 증거 → 검증
    Bedrock-->>Agent: B: REJECTED (0.1)
    Agent->>Bedrock: 가설 C + 증거 → 검증
    Bedrock-->>Agent: C: REJECTED (0.15)

    Note over Agent,Bedrock: Phase 6: F7 종료 판단 + F6 분기
    Agent->>Agent: 종료 조건 미충족 → 계속
    Agent->>Bedrock: 가설 A 하위 분기 요청
    Bedrock-->>Agent: 하위 가설 생성
    Note right of Agent: A-1: 커넥션 풀 설정 변경 (0.4)<br/>A-2: 코드에서 커넥션 미반환 (0.7)

    Note over Agent,CT_MCP: Loop 2: F3→F4→F5
    Agent->>DDB: state = HYPOTHESIS_PRIORITIZATION
    Agent->>DDB: state = EVIDENCE_COLLECTION

    rect rgb(232, 245, 233)
        Note over Agent,GH_MCP: A-1, A-2 증거 수집
        Agent->>CT_MCP: 배포 변경 상세 조회
        CT_MCP-->>Agent: RegisterTaskDefinition 이벤트 상세
        Agent->>GH_MCP: 배포 커밋 diff 조회 (get_commit)
        GH_MCP-->>Agent: db.py에서 connection.close() 제거 확인
        Agent->>CW_MCP: 커넥션 추이 상세 분석
        CW_MCP-->>Agent: 배포 시점부터 정확히 선형 증가<br/>(풀 설정 변경 아닌 누수 패턴)
        Agent->>S3: 증거 아카이브 저장
    end

    Agent->>DDB: state = HYPOTHESIS_VALIDATION
    Agent->>Bedrock: A-1 + 증거 → 검증
    Bedrock-->>Agent: A-1: REJECTED (0.2)
    Agent->>Bedrock: A-2 + 증거 → 검증
    Bedrock-->>Agent: A-2: CONFIRMED (0.92)

    Note over Agent,Bedrock: 종료 → confidence ≥ 0.9

    Note over Agent,S3: Phase 7: F8 보고서 생성
    Agent->>DDB: state = REPORT_GENERATION
    Agent->>Bedrock: RCA 보고서 작성 요청
    Bedrock-->>Agent: 구조화된 보고서
    Agent->>S3: reports/{rca_id}.md 저장

    Note over Agent,S3V: Phase 8: F9 플레이북 생성
    Agent->>S3V: 기존 유사 플레이북 검색 (≥0.86)
    S3V-->>Agent: (해당 없음 → 신규 생성)
    Agent->>Bedrock: 플레이북 생성 요청
    Bedrock-->>Agent: DB 커넥션 누수 플레이북
    Agent->>S3V: 플레이북 임베딩 인덱싱

    Note over Agent,SNS: Phase 9: F10 알림
    Agent->>S3: presigned URL 생성
    Agent->>SNS: RCA 완료 알림 발행
    Agent->>DDB: state = COMPLETED
    SNS-->>SNS: SRE 팀 수신
```

### 각 Phase별 산출물

| Phase | 단계 | 주요 산출물 | 저장소 |
|-------|------|-----------|--------|
| 0 | 알람 수신 | AlarmPayload, RCA 세션 | DynamoDB |
| 1 | F1 스코핑 | ScopingResult (severity=high, blast=multi) | - |
| 2 | F2 가설 생성 | 가설 A/B/C (3개) | - |
| 3 | F3 우선순위 | A→B→C 검증 순서 | - |
| 4 | F4 증거 수집 | 메트릭(커넥션 추이), 로그(Too many connections), 배포 이력, 코드 diff | S3 |
| 5 | F5 검증 (1차) | A: NEEDS_INVESTIGATION, B/C: REJECTED | DynamoDB |
| 6 | F6 분기 | A-1(풀 설정), A-2(커넥션 미반환) | - |
| 4-5 | F4-F5 (2차) | A-1: REJECTED, A-2: CONFIRMED (0.92) | S3, DynamoDB |
| 7 | F8 보고서 | RCA Report (Markdown) | S3 |
| 8 | F9 플레이북 | DB 커넥션 누수 대응 플레이북 | S3 Vectors |
| 9 | F10 알림 | SNS 알림 (presigned URL 포함) | SNS → SRE |

### 데모에서 사용되는 MCP 도구

| MCP 서버 | 도구 | 용도 |
|---------|------|------|
| CloudWatch MCP | `get_metric_data` | DB 커넥션 수, Latency, RequestCount, CPU 메트릭 조회 |
| CloudWatch MCP | `execute_log_insights_query` | "Too many connections" 에러 로그 검색 |
| CloudWatch MCP | `analyze_metric` | 커넥션 증가 트렌드 분석 |
| CloudTrail MCP | `lookup_events` | ECS 배포 이벤트(RegisterTaskDefinition) 조회 |
| GitHub MCP | `get_commit`, `list_commits` | 배포 커밋 diff 조회, 결함 패턴 탐지 |
| GitHub MCP | `pull_request_read` | PR diff, 변경 파일 목록, 리뷰 코멘트 조회 |

### 종료 조건 매핑

이 데모에서는 **CONFIRMED** 종료 조건이 트리거됩니다:
- 가설 A-2 "코드에서 커넥션 미반환"이 confidence 0.92로 확정
- 임계치 0.9 이상 → 즉시 종료 → 보고서 생성 단계 진입

## 5. 에이전트 모델 티어 매핑

### Fargate Stack (Strands) — 2-Tier

```mermaid
flowchart LR
    subgraph Planning["Planning Tier<br/>(Sonnet 4.6 + Adaptive Thinking)"]
        HYP["F2: Hypothesis Gen"]
        PRIO["F3: Prioritization"]
        BRANCH["F6: Branching"]
        REPORT["F8: Report"]
        PLAYBOOK["F9: Playbook"]
    end

    subgraph Execution["Execution Tier<br/>(Haiku 4.5)"]
        SCOPING["F1: Scoping"]
        EVIDENCE["F4: Evidence Collection"]
        VALIDATION["F5: Validation"]
    end

    subgraph NoLLM["순수 로직 (LLM 미사용)"]
        TERM["F7: Termination"]
        NOTIF["F10: Notification"]
    end

    subgraph MCP["MCP 서버 연결"]
        CW["CloudWatch MCP"]
        CT["CloudTrail MCP"]
        GH["GitHub MCP"]
    end

    SCOPING -.-> CW
    SCOPING -.-> CT
    EVIDENCE -.-> CW
    EVIDENCE -.-> CT
    EVIDENCE -.-> GH

    style Planning fill:#e3f2fd,stroke:#1565c0
    style Execution fill:#e8f5e9,stroke:#2e7d32
    style NoLLM fill:#f5f5f5,stroke:#616161
    style MCP fill:#fff3e0,stroke:#ef6c00
```

### Lambda Stack (CC Headless) — 단일 모델

```mermaid
flowchart LR
    subgraph CCModel["CC Headless<br/>(Sonnet 4.6 via Bedrock)"]
        CC["Claude Code CLI<br/>프롬프트 주도 RCA"]
    end

    subgraph MCP["MCP 서버 연결"]
        CW["CloudWatch MCP"]
        CT["CloudTrail MCP"]
        GH["GitHub MCP"]
    end

    subgraph Lambda["Lambda 핸들러<br/>(LLM 미사용)"]
        PARSE["알람 파싱"]
        SESSION["세션 관리"]
        REPORT_S["보고서 저장"]
        NOTIFY["SNS 알림"]
    end

    CC -.-> CW
    CC -.-> CT
    CC -.-> GH
    PARSE --> CC --> REPORT_S --> NOTIFY

    style CCModel fill:#e3f2fd,stroke:#1565c0
    style MCP fill:#fff3e0,stroke:#ef6c00
    style Lambda fill:#f5f5f5,stroke:#616161
```
