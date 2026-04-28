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
        S_AGENT["Scoping Agent<br/>(Execution 티어: Haiku 4.5)<br/>AWS Knowledge + CW + CT MCP"]
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
            E_AGENT["Evidence Agent<br/>(Execution 티어: Haiku 4.5)<br/>가설별 독립 Agent 인스턴스"]
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
            V_AGENT["Validation Agent<br/>(Execution 티어: Haiku 4.5)<br/>ThreadPoolExecutor 병렬 실행"]
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

    subgraph Execution["Execution 티어<br/>(Haiku 4.5)"]
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

## 2. Fargate Stack (CC Headless) — 프롬프트 주도

### 2.1. 전체 플로우

CC on Bedrock headless 모드에서 단일 프롬프트로 전체 RCA를 수행합니다. Python 핸들러가 SQS 수신/세션 관리를 담당하고, CC CLI subprocess가 MCP 도구를 자율적으로 호출하며 RCA를 진행합니다. Artifact Watcher 스레드가 `/tmp/rca-{id}/` 디렉토리를 감시하여 산출물 파일이 생성될 때마다 DynamoDB에 트레이스를 기록합니다.

```mermaid
flowchart TD
    subgraph Input["입력 & 사전 검증"]
        SQS["SQS Long Polling<br/>(WaitTimeSeconds=20)"]
        PARSE["AlarmPayload 파싱<br/>(SNS envelope unwrap)"]
        DEDUP["멱등성 체크<br/>(DynamoDB IDEMP# 키)"]
        STALE["Stale 알람 체크<br/>(30분 초과 → OUTDATED)"]
        SESSION["세션 생성<br/>(engine: cc-headless)"]
        SQS --> PARSE --> DEDUP --> STALE --> SESSION
    end

    subgraph Prepare["실행 준비"]
        ARTIFACT_DIR["/tmp/rca-{rca_id}/ 생성"]
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
        PROGRESS["rca-progress MCP<br/>save_artifact() 도구"]
    end

    subgraph RCA["CC 프롬프트 내 11단계 RCA 워크플로우"]
        direction TB
        STEP1["1. 초기 스코핑<br/>→ scoping.json"]
        STEP2["2. 가설 생성 (서브에이전트)<br/>→ hypotheses.json"]
        STEP3["3-7. 검증 루프 (서브에이전트, 최대 3회)<br/>우선순위 → 빔 선택 → 증거 수집 →<br/>검증 → 분기 / 재생성<br/>→ validation-N.json"]
        STEP4["8. 보고서 생성<br/>→ report.md"]
        STEP5["9. 플레이북 생성<br/>→ playbook.json"]
        STEP6["10. 자동 복구<br/>(fault reset API / ECS 강제 배포)"]
        STEP7["11. 복구 검증<br/>(메트릭 재조회)"]
        STEP1 --> STEP2 --> STEP3 --> STEP4 --> STEP5 --> STEP6 --> STEP7
    end

    subgraph WatcherDetail["Artifact Watcher (백그라운드)"]
        W_DETECT["파일 감지<br/>(scoping.json, hypotheses.json,<br/>validation-N.json, report.md,<br/>playbook.json)"]
        W_SPAN["DDB SPAN 레코드 생성"]
        W_HYPO["DDB HYPO 레코드<br/>생성/갱신"]
        W_DETECT --> W_SPAN
        W_DETECT --> W_HYPO
    end

    subgraph Output["결과 처리"]
        REPORT_PARSE["report.md 읽기<br/>+ 근본원인 추출 (regex)"]
        S3_REPORT["S3 보고서 저장<br/>(reports/{rca_id}.md)"]
        PB_PARSE["playbook.json 파싱<br/>→ S3 Vectors 인덱싱"]
        SNS_NOTIFY["SNS 알림 발행<br/>(presigned URL + 플레이북)"]
        DDB_COMPLETE["DDB 세션 갱신<br/>(state → COMPLETED,<br/>root_cause 저장)"]
        DEL_MSG["SQS 메시지 삭제"]
        REPORT_PARSE --> S3_REPORT --> PB_PARSE --> SNS_NOTIFY --> DDB_COMPLETE --> DEL_MSG
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

CC Headless는 Strands와 달리 단 2개의 활성 상태만 갖습니다. 파이프라인 내부 진행 상황은 Artifact Watcher가 SPAN/HYPO 레코드로 DynamoDB에 기록합니다.

```mermaid
stateDiagram-v2
    [*] --> ALARM_RECEIVED: SQS Long Polling + 세션 생성

    ALARM_RECEIVED --> [*]: 중복 감지 → 즉시 반환
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
        · report.md → REPORT 스팬
        · playbook.json → PLAYBOOK 스팬
    end note

    COMPLETED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
```

### 2.3. CC 프롬프트 내 11단계 워크플로우 상세

CC CLI가 자율적으로 수행하는 파이프라인입니다. 메인 에이전트가 직접 수행하는 단계와 서브에이전트에게 위임하는 단계로 구분됩니다.

```mermaid
flowchart TD
    subgraph Direct1["메인 에이전트 직접 수행"]
        S1["1. 초기 스코핑<br/>· AWS Knowledge MCP: 장애 패턴 검색<br/>· CloudWatch MCP: 알람 메트릭 30분 + 24시간 비교<br/>· 영향범위/심각도 판정"]
        S1_OUT["save_artifact('scoping.json')"]
        S1 --> S1_OUT
    end

    subgraph SubAgent1["서브에이전트 위임"]
        S2["2. 가설 생성<br/>· Agent tool로 서브에이전트 스폰<br/>· 3-5개 가설 생성 (UUID 부여)"]
        S2_OUT["save_artifact('hypotheses.json')"]
        S2 --> S2_OUT
    end

    subgraph SubAgent2["서브에이전트 위임 (루프)"]
        S37["3-7. 검증 루프 (최대 3회)<br/>매 루프마다 서브에이전트 스폰:<br/>· 우선순위 결정 + 빔 선택 (상위 3)<br/>· 증거 수집 (CW/CT/GH MCP)<br/>· 검증 (≥0.8 CONFIRMED, ≤0.3 REJECTED)<br/>· 분기 (NEEDS_INVESTIGATION → 하위 가설)"]
        S37_OUT["save_artifact('validation-N.json')"]
        S37_TERM{{"종료 조건?"}}
        S37_REGEN["전체 기각 → 재생성 (최대 2회)"]
        S37 --> S37_OUT --> S37_TERM
        S37_TERM -->|신뢰도 ≥ 0.9| Direct2
        S37_TERM -->|시간 > 8분| Direct2
        S37_TERM -->|루프 3회 완료| Direct2
        S37_TERM -->|all_rejected| S37_REGEN
        S37_REGEN --> S37
        S37_TERM -->|계속| S37
    end

    subgraph Direct2["메인 에이전트 직접 수행"]
        S8["8. 보고서 생성<br/>· Markdown RCA 보고서 작성<br/>· 인시던트 요약, 근본원인, 증거,<br/>  가설 경로, 조치 방안, 타임라인"]
        S8_OUT["save_artifact('report.md')"]
        S9["9. 플레이북 생성<br/>· failure_type, symptom_pattern<br/>· verification_steps, mitigation, remediation"]
        S9_OUT["save_artifact('playbook.json')"]
        S10["10. 자동 복구<br/>· 장애 유형별 fault reset API 호출<br/>· 매칭 없으면 ECS 강제 새 배포"]
        S11["11. 복구 검증<br/>· 30초 대기 후 메트릭 재조회<br/>· 정상화 여부 확인<br/>· 보고서에 검증 결과 추가"]
        S8 --> S8_OUT --> S9 --> S9_OUT --> S10 --> S11
    end

    S1_OUT --> SubAgent1
    S2_OUT --> SubAgent2

    style Direct1 fill:#e8f5e9,stroke:#2e7d32
    style SubAgent1 fill:#e3f2fd,stroke:#1565c0
    style SubAgent2 fill:#e3f2fd,stroke:#1565c0
    style Direct2 fill:#e8f5e9,stroke:#2e7d32
```

### 2.4. MCP 서버 구성

| MCP 서버 | 실행 방식 | 용도 |
|---------|----------|------|
| `aws-knowledge` | `uvx fastmcp run https://knowledge-mcp.global.api.aws` | AWS 서비스 문서 참조, 장애 패턴 검색 |
| `cloudwatch` | `uvx awslabs.cloudwatch-mcp-server` | 메트릭 조회, Logs Insights 쿼리, 알람 조회 |
| `cloudtrail` | `uvx awslabs.cloudtrail-mcp-server` | 배포/변경 이벤트 조회, Lake SQL 분석 |
| `github` | `github-mcp-server stdio` | 커밋 diff, PR diff, 파일 내용 조회 |
| `rca-progress` | `python -m fastmcp run mcp_server.py:mcp` | `save_artifact(filename, content)` — 산출물 파일 저장 |

### 2.5. Artifact Watcher 파일 → DDB 매핑

| 파일 | DDB 스팬 타입 | 추가 동작 |
|------|-------------|----------|
| `scoping.json` | `SCOPING` | — |
| `hypotheses.json` | `HYPOTHESIS_GENERATION` | HYPO# 레코드 batch write (최대 25개) |
| `validation-N.json` | `VALIDATION_LOOP` | HYPO# 레코드 상태 갱신 (confirmed/rejected/closed/needs_investigation) |
| `report.md` | `REPORT` | — |
| `playbook.json` | `PLAYBOOK` | failure_type, tags 등 메타데이터 저장 |

---

## 3. 두 스택 비교

| | Fargate Stack (Strands) | Fargate Stack (CC Headless) |
|---|---|---|
| **실행 환경** | ECS Fargate (Long Polling) | ECS Fargate (Long Polling) |
| **에이전트 엔진** | Strands Agents SDK (Python) | Claude Code CLI (headless, Bedrock) |
| **RCA 방식** | 9단계 코드 기반 파이프라인 | 단일 프롬프트 + 11단계 자율 수행 |
| **모델** | 2-tier (Sonnet 4.6 + Haiku 4.5) | CC 기본 모델 (Sonnet 4.6) |
| **서브에이전트** | Strands Agent 인스턴스 (코드로 생성) | CC Agent tool (프롬프트로 스폰) |
| **상태 관리** | Python 코드가 매 단계 DDB 업데이트 | Artifact Watcher가 파일 감시 → DDB 기록 |
| **DDB 상태 수** | 7개 활성 상태 + 4개 terminal | 2개 활성 상태 + 4개 terminal |
| **자동 복구** | 미구현 (ADR 0012, 모듈만 준비) | 프롬프트 내 10-11단계로 직접 수행 |
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
    Agent->>Bedrock: 가설 A + 증거 요약 → 검증 (Haiku 4.5)
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
    Agent->>Bedrock: A-1 + 증거 → 검증 (Haiku 4.5)
    Bedrock-->>Agent: A-1: REJECTED (0.2)
    Agent->>Bedrock: A-2 + 증거 → 검증 (Haiku 4.5)
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
REPORT (report.md 감지 시)
PLAYBOOK (playbook.json 감지 시)
```
