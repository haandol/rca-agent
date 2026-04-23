# RCA Agent 시스템 운영 가이드

> 주니어 DevOps 운영팀원을 위한 시스템 아키텍처, 데이터 흐름, 데모 시나리오 안내 문서

## 목차

1. [시스템이 하는 일](#1-시스템이-하는-일)
2. [전체 아키텍처](#2-전체-아키텍처)
3. [AWS 인프라 구성](#3-aws-인프라-구성)
4. [데이터 흐름 — 알람부터 보고서까지](#4-데이터-흐름--알람부터-보고서까지)
5. [두 가지 RCA 엔진 비교](#5-두-가지-rca-엔진-비교)
6. [Fargate 엔진 — 12단계 파이프라인](#6-fargate-엔진--12단계-파이프라인)
7. [CC Headless 엔진 — ECS Fargate](#7-cc-headless-엔진--ecs-fargate)
8. [MCP 서버 — 외부 데이터 수집 도구](#8-mcp-서버--외부-데이터-수집-도구)
9. [Healthcare Sensor App — 데모용 서비스](#9-healthcare-sensor-app--데모용-서비스)
10. [데모 시나리오 1: DB 커넥션 누수](#10-데모-시나리오-1-db-커넥션-누수)
11. [데모 시나리오 2: CPU 과부하](#11-데모-시나리오-2-cpu-과부하)
12. [데모 시나리오 3: Slow Query](#12-데모-시나리오-3-slow-query)
13. [세션 상태와 DynamoDB](#13-세션-상태와-dynamodb)
14. [장애 대응 체크리스트](#14-장애-대응-체크리스트)
15. [부록: 데모 실행 가이드](#15-부록-데모-실행-가이드)

---

## 1. 시스템이 하는 일

CloudWatch 알람이 발생하면, AI 에이전트가 **자동으로 근본원인분석(RCA)**을 수행합니다.

```mermaid
flowchart LR
    A["🔔 CloudWatch 알람 발생"] --> B["🤖 AI 에이전트가 자동 분석"]
    B --> C["📊 메트릭/로그/배포이력 수집"]
    C --> D["🔍 가설 생성 → 검증 → 확정"]
    D --> E["📝 RCA 보고서 생성"]
    E --> F["📨 SRE 팀에 알림 전송"]
```

**핵심 가치**: 장애 발생 시 SRE가 직접 CloudWatch 콘솔을 뒤지고, 로그를 검색하고, 배포 이력을 추적하는 작업을 AI가 대신 수행합니다. 보통 30분~1시간 걸리는 초기 분석을 **1~5분** 내에 자동 완료합니다.

---

## 2. 전체 아키텍처

이 시스템은 **동일한 알람에 대해 두 가지 독립적인 RCA 엔진**이 동시에 분석을 수행하는 **Dual-Stack** 구조입니다.

```mermaid
graph TB
    subgraph EventSource["이벤트 소스"]
        CW["☁️ CloudWatch Alarm"]
    end

    subgraph Routing["이벤트 라우팅 (SNS → SQS)"]
        SNS["SNS Topic<br/>(알람 팬아웃)"]
        SQS_F["SQS Queue #1<br/>(Fargate용)"]
        SQS_L["SQS Queue #2<br/>(CC Headless용)"]
    end

    subgraph DualStack["Dual-Stack RCA 엔진"]
        subgraph FargateStack["🟦 Fargate Stack"]
            ECS["ECS Fargate Task<br/>Python · Strands SDK<br/>12단계 파이프라인"]
        end
        subgraph CcHeadlessStack["🟧 CC Headless Stack"]
            CCFARGATE["ECS Fargate<br/>Node.js · Claude Code CLI<br/>프롬프트 주도 RCA"]
        end
    end

    subgraph Tools["외부 데이터 수집 (MCP 서버)"]
        AK["AWS Knowledge MCP<br/>AWS 문서/트러블슈팅 가이드"]
        CW_MCP["CloudWatch MCP<br/>메트릭 · 로그"]
        CT_MCP["CloudTrail MCP<br/>배포 · 변경 이력"]
    end

    subgraph LLM["AI 모델 (Amazon Bedrock)"]
        SONNET["Sonnet 4.6<br/>(추론·판단)"]
        HAIKU["Haiku 4.5<br/>(데이터 수집)"]
    end

    subgraph SharedStorage["공유 저장소"]
        DDB["DynamoDB<br/>세션 상태 + 멱등성"]
        S3["S3<br/>증거 · 보고서"]
        S3V["S3 Vectors<br/>플레이북 임베딩"]
    end

    subgraph Notify["알림"]
        SNS_OUT["SNS Topic<br/>(RCA 완료)"]
        SRE["👩‍💻 SRE 팀"]
    end

    CW --> SNS
    SNS --> SQS_F --> ECS
    SNS --> SQS_L --> LAMBDA

    ECS <--> SONNET
    ECS <--> HAIKU
    LAMBDA <--> SONNET

    ECS --> AK
    ECS --> CW_MCP
    ECS --> CT_MCP
    LAMBDA --> AK
    LAMBDA --> CW_MCP
    LAMBDA --> CT_MCP

    ECS --> DDB
    ECS --> S3
    ECS <--> S3V
    LAMBDA --> DDB
    LAMBDA --> S3

    ECS --> SNS_OUT
    LAMBDA --> SNS_OUT
    SNS_OUT --> SRE

    style FargateStack fill:#e3f2fd,stroke:#1565c0
    style CcHeadlessStack fill:#fff3e0,stroke:#ef6c00
    style Tools fill:#e8f5e9,stroke:#388e3c
```

**왜 두 개의 엔진을 사용하나요?**

| | Fargate (Strands) | Fargate (CC Headless) |
|---|---|---|
| 장점 | 정교한 12단계 분석, 플레이북 학습 | 프롬프트 주도로 유연, 코드 간단 |
| 단점 | 항시 실행, 비용 발생 | 동작이 덜 예측 가능 |
| 용도 | 정밀 분석이 필요한 복잡한 장애 | 빠른 초기 대응, 간단한 장애 |

---

## 3. AWS 인프라 구성

전체 인프라는 AWS CDK로 관리되며, 9개의 스택으로 구성됩니다.

```mermaid
graph TB
    subgraph Infra["CDK 스택 의존관계"]
        ECR["📦 EcrStack<br/>ECR 리포지토리 3개"]
        NET["🌐 NetworkStack<br/>VPC + Subnet + NAT"]
        EVENT["📡 EventBusStack<br/>SNS + SQS + DLQ"]
        DB["💾 DatabaseStack<br/>DynamoDB"]
        STORAGE["🗄️ StorageStack<br/>S3 + S3 Vectors"]
        RDS["🐘 RdsStack<br/>PostgreSQL 17.4"]
        AGENT["🟦 RcaAgentServiceStack<br/>ECS Fargate (RCA)"]
        CC["🟧 CcHeadlessStack<br/>ECS Fargate"]
        HEALTH["🏥 HealthcareServiceStack<br/>ECS Fargate (데모)"]
    end

    ECR --> AGENT
    ECR --> CC
    ECR --> HEALTH
    NET --> EVENT
    NET --> AGENT
    NET --> CC
    NET --> HEALTH
    NET --> RDS
    EVENT --> AGENT
    EVENT --> CC
    DB --> AGENT
    DB --> CC
    STORAGE --> AGENT
    STORAGE --> CC
    RDS --> HEALTH

    style ECR fill:#f3e5f5,stroke:#7b1fa2
    style NET fill:#e3f2fd,stroke:#1565c0
    style EVENT fill:#fff3e0,stroke:#ef6c00
    style DB fill:#fce4ec,stroke:#c62828
    style STORAGE fill:#e8f5e9,stroke:#388e3c
    style RDS fill:#fce4ec,stroke:#c62828
```

### 주요 리소스 요약

| 리소스 | 용도 | 스펙 |
|--------|------|------|
| **VPC** | 모든 서비스의 네트워크 | Public + Private Subnet, NAT Gateway |
| **SNS (알람 수신)** | CloudWatch 알람 팬아웃 | 1개 토픽 → 2개 SQS로 분배 |
| **SQS (Fargate용)** | Fargate Long Polling | visibility=25분, retention=4일, DLQ 연결 |
| **SQS (CC Headless용)** | CC Headless Long Polling | visibility=35분, retention=4일, DLQ 연결 |
| **DynamoDB** | RCA 세션 상태 관리 | PAY_PER_REQUEST, PITR, TTL, GSI(멱등성) |
| **S3 (Evidence)** | 수집 증거 + 보고서 저장 | 60일 lifecycle, S3 managed encryption |
| **S3 Vectors** | 플레이북 임베딩 검색 | cosine 유사도, 1024차원 벡터 |
| **ECS Fargate** | RCA Agent + Healthcare App | ARM64, 1vCPU, 2GB RAM |
| **ECS Fargate (CC Headless)** | CC Headless RCA | ARM64, 1vCPU, 2GB RAM |
| **RDS PostgreSQL** | Healthcare 센서 데이터 | PostgreSQL 17.4 |
| **ECR** | Docker 이미지 레지스트리 | rca-agent, cc-headless, healthcare 3개 |

---

## 4. 데이터 흐름 — 알람부터 보고서까지

하나의 CloudWatch 알람이 발생했을 때 시스템 전체를 관통하는 데이터 흐름입니다.

```mermaid
sequenceDiagram
    participant CW as CloudWatch<br/>Alarm
    participant SNS as SNS Topic<br/>(알람 팬아웃)
    participant SQS1 as SQS Queue #1<br/>(Fargate용)
    participant SQS2 as SQS Queue #2<br/>(CC Headless용)
    participant ECS as ECS Fargate<br/>(Strands 에이전트)
    participant CCH as ECS Fargate<br/>(CC Headless)
    participant DDB as DynamoDB<br/>(세션 테이블)
    participant MCP as MCP 서버들<br/>(CW·CT·Knowledge)
    participant BED as Amazon Bedrock<br/>(AI 모델)
    participant S3 as S3<br/>(증거·보고서)
    participant NOTIFY as SNS → SRE

    Note over CW,NOTIFY: ① 알람 발생 및 라우팅
    CW->>SNS: 알람 메시지 발행
    SNS->>SQS1: 복제 (Fargate용)
    SNS->>SQS2: 복제 (CC Headless용)

    Note over ECS,DDB: ② 멱등성 체크 (중복 방지)
    par Fargate
        SQS1->>ECS: Long Polling으로 수신
        ECS->>DDB: 중복 체크 (IDEMP# 키)
        DDB-->>ECS: 신규 → 세션 생성
    and CC Headless
        SQS2->>CCH: Long Polling으로 수신
        CCH->>DDB: 중복 체크 (IDEMP# 키)
        DDB-->>CCH: 신규 → 세션 생성
    end

    Note over ECS,BED: ③ RCA 분석 수행 (두 엔진 독립 실행)
    par Fargate 분석
        ECS->>MCP: 메트릭·로그·배포이력 수집
        MCP-->>ECS: 데이터 반환
        ECS->>BED: 가설 생성·검증·보고서 요청
        BED-->>ECS: AI 응답
        ECS->>S3: 증거 + 보고서 저장
        ECS->>DDB: 상태 갱신 (COMPLETED)
    and CC Headless 분석
        CCH->>MCP: 메트릭·로그·배포이력 수집
        MCP-->>CCH: 데이터 반환
        CCH->>BED: 프롬프트 주도 분석
        BED-->>CCH: AI 응답
        CCH->>S3: 보고서 저장
        CCH->>DDB: 상태 갱신 (COMPLETED)
    end

    Note over ECS,NOTIFY: ④ 알림 발송
    ECS->>NOTIFY: RCA 완료 알림 (presigned URL)
    CCH->>NOTIFY: RCA 완료 알림 (presigned URL)
```

**핵심 포인트**:
- SNS 팬아웃으로 **하나의 알람이 두 SQS 큐에 동시 전달**됩니다
- 각 엔진은 **DynamoDB IDEMP# 키**로 같은 알람을 중복 처리하지 않습니다 (자기 엔진 내에서)
- 두 엔진은 서로 독립적으로 동작하며, `engine` 필드(`strands` vs `cc-headless`)로 구분됩니다
- 보고서는 **S3 presigned URL**로 SRE 팀에 전달됩니다

---

## 5. 두 가지 RCA 엔진 비교

```mermaid
graph LR
    subgraph Fargate["🟦 Fargate Stack (Strands)"]
        direction TB
        F_ENV["ECS Fargate<br/>Python 3.12"]
        F_SDK["Strands Agents SDK<br/>12단계 파이프라인"]
        F_MODEL["2-Tier 모델<br/>Sonnet 4.6 (추론)<br/>Haiku 4.5 (수집)"]
        F_TIME["시간 제한 없음<br/>(ECS 무제한)"]
        F_PLAY["✅ 플레이북 생성/학습"]
    end

    subgraph CcStack["🟧 CC Headless Stack (ECS Fargate)"]
        direction TB
        L_ENV["ECS Fargate<br/>Node.js 22"]
        L_SDK["Claude Code CLI<br/>프롬프트 주도"]
        L_MODEL["단일 모델<br/>Sonnet 4.6"]
        L_TIME["타임아웃 없음<br/>(ECS 무제한)"]
        L_PLAY["❌ 플레이북 미지원"]
    end

    style Fargate fill:#e3f2fd,stroke:#1565c0
    style CcStack fill:#fff3e0,stroke:#ef6c00
```

| 항목 | Fargate (Strands) | Fargate (CC Headless) |
|------|-------------------|---------------------|
| **실행 환경** | ECS Fargate (항시 실행) | ECS Fargate (항시 실행) |
| **에이전트 프레임워크** | Strands Agents SDK (Python) | Claude Code CLI (Node.js) |
| **RCA 방식** | 12단계 파이프라인 (코드로 정의) | 프롬프트에 워크플로우 정의, CC가 자율 실행 |
| **AI 모델** | Sonnet 4.6 + Haiku 4.5 (2-Tier) | Sonnet 4.6 (단일) |
| **분석 깊이** | 가설 트리 탐색 (depth 최대 5) | 프롬프트 기반 (depth 최대 3) |
| **플레이북** | 생성 + S3 Vectors 인덱싱 | 미지원 |
| **이벤트 수신** | SQS Long Polling | SQS Long Polling |
| **타임아웃** | 없음 (종료조건 기반) | 없음 (ECS 무제한) |
| **동시성** | Fargate 태스크 스케일링 | Fargate 태스크 스케일링 |
| **비용 모델** | 항시 실행 비용 | 항시 실행 비용 |

---

## 6. Fargate 엔진 — 12단계 파이프라인

Strands SDK 기반 Fargate 엔진은 RCA를 체계적인 12단계로 수행합니다.

```mermaid
flowchart TD
    subgraph Input["① 입력"]
        SQS["SQS 메시지 수신"]
        PARSE["AlarmPayload 파싱"]
        DEDUP["멱등성 체크"]
        SQS --> PARSE --> DEDUP
    end

    subgraph Phase1["② 초기 분석"]
        F1["F1: 스코핑<br/>🔧 Haiku 4.5<br/>메트릭 수집 + 심각도 판정"]
        F2["F2: 가설 생성<br/>🧠 Sonnet 4.6<br/>3~5개 가설 생성"]
    end

    subgraph Loop["③ 검증 루프 (최대 3회 반복)"]
        F3["F3: 우선순위 결정<br/>🧠 Sonnet 4.6"]
        F4["F4: 증거 수집<br/>🔧 Haiku 4.5<br/>CW·CT·GitHub MCP"]
        F5["F5: 가설 검증<br/>🧠 Sonnet 4.6<br/>CONFIRMED / REJECTED /<br/>NEEDS_INVESTIGATION"]
        F7{"F7: 종료 판단<br/>(순수 로직)"}
        F6["F6: 분기<br/>🧠 Sonnet 4.6<br/>하위 가설 생성"]

        F3 --> F4 --> F5 --> F7
        F7 -->|"계속 탐색"| F6
        F6 -->|"하위 가설 추가"| F3
    end

    subgraph Output["④ 결과 생성"]
        F8["F8: 보고서 작성<br/>🧠 Sonnet 4.6<br/>→ S3 저장"]
        F9["F9: 플레이북 생성<br/>🧠 Sonnet 4.6<br/>→ S3 Vectors 인덱싱"]
        F10["F10: 자동 복구<br/>fault reset API<br/>ECS force deploy"]
        F11["F11: 복구 검증<br/>🔧 Haiku 4.5<br/>CloudWatch MCP 메트릭 확인"]
        F12["F12: SNS 알림<br/>(presigned URL)"]
        F8 --> F9 --> F10 --> F11 --> F12
    end

    DEDUP --> F1
    F1 --> F2
    F2 --> F3
    F7 -->|"종료"| F8

    style Phase1 fill:#e3f2fd,stroke:#1565c0
    style Loop fill:#fff8e1,stroke:#f9a825
    style Output fill:#e8f5e9,stroke:#388e3c
```

### 종료 조건 (5가지 중 하나라도 충족 시 종료)

```mermaid
graph LR
    subgraph Conditions["종료 조건 (OR 연산)"]
        C1["✅ 가설 confidence ≥ 0.9<br/>(근본 원인 확정)"]
        C2["⏰ 분석 시간 ≥ 20분"]
        C3["🌲 가설 트리 깊이 > 5"]
        C4["🔄 검증 루프 > 3회"]
        C5["❌ 모든 가설 기각<br/>(재생성 최대 2회 후)"]
    end
    
    Conditions --> STOP["종료 → F8 보고서 생성"]
```

### 2-Tier 모델 아키텍처

비용과 성능을 최적화하기 위해 두 가지 모델 티어를 사용합니다.

```mermaid
graph TB
    subgraph Planning["🧠 Planning Tier — Sonnet 4.6"]
        P1["F2: 가설 생성"]
        P2["F3: 우선순위 결정"]
        P3["F5: 가설 검증"]
        P4["F6: 분기"]
        P5["F8: 보고서 작성"]
        P6["F9: 플레이북 생성"]
    end

    subgraph Execution["🔧 Execution Tier — Haiku 4.5"]
        E1["F1: 스코핑<br/>(MCP 도구 호출)"]
        E2["F4: 증거 수집<br/>(MCP 도구 호출)"]
        E3["F11: 복구 검증<br/>(MCP 도구 호출)"]
    end

    subgraph NoLLM["⚙️ 순수 로직 (AI 미사용)"]
        N1["F7: 종료 판단"]
        N2["F12: SNS 알림"]
        N3["F10: 자동 복구"]
    end

    style Planning fill:#e3f2fd,stroke:#1565c0
    style Execution fill:#e8f5e9,stroke:#388e3c
    style NoLLM fill:#f5f5f5,stroke:#9e9e9e
```

- **Planning Tier** (Sonnet 4.6): 추론·판단이 필요한 단계 → 고성능 모델, 비용 높음
- **Execution Tier** (Haiku 4.5): 도구 호출·데이터 수집 → 가벼운 모델, 비용 낮음, 응답 빠름
- **순수 로직**: AI 불필요 → 코드로 직접 처리

---

## 7. CC Headless 엔진 — ECS Fargate

Claude Code CLI를 ECS Fargate에서 headless 모드로 실행하여, 프롬프트 하나로 전체 RCA를 수행합니다.

```mermaid
flowchart TD
    subgraph EcsHandler["ECS Handler (Node.js)"]
        SQS["SQS Event 수신"]
        PARSE["알람 파싱"]
        IDEMP["멱등성 체크<br/>(DynamoDB)"]
        SESSION["세션 생성"]
        SQS --> PARSE --> IDEMP --> SESSION
    end

    subgraph CCProcess["Claude Code CLI (서브프로세스)"]
        CC["claude -p &lt;prompt&gt;<br/>--output-format json<br/>--mcp-config mcp-config.json<br/>--max-turns 30"]

        subgraph Prompt["프롬프트 내 RCA 워크플로우"]
            S1["Step 1: 초기 스코핑 (2분)"]
            S2["Step 2: 가설 생성 (2분)"]
            S3["Step 3: 증거 수집 + 검증 루프 (4분)"]
            S4["Step 4: 종료 판단 (1분)"]
            S5["Step 5: 보고서 생성 (1분)"]
            S1 --> S2 --> S3 --> S4 --> S5
        end

        CC --> Prompt
    end

    subgraph MCPServers["MCP 서버 (CC가 자율 호출)"]
        AK["AWS Knowledge MCP<br/>AWS 문서 검색"]
        CW["CloudWatch MCP<br/>메트릭·로그"]
        CT["CloudTrail MCP<br/>배포·변경 이력"]
    end

    subgraph Output["결과 처리"]
        RESULT["JSON 결과 파싱"]
        S3_SAVE["S3 보고서 저장"]
        SNS_SEND["SNS 알림 발송"]
        DDB_UP["DynamoDB 상태 갱신"]
        RESULT --> S3_SAVE --> SNS_SEND --> DDB_UP
    end

    SESSION --> CC
    CC <--> MCPServers
    CC --> RESULT

    style CCProcess fill:#fff3e0,stroke:#ef6c00
    style MCPServers fill:#e8f5e9,stroke:#388e3c
    style Prompt fill:#f3e5f5,stroke:#7b1fa2
```

**Fargate와의 차이점**:
- Fargate는 각 단계를 **Python 코드로** 명시적으로 구현
- CC Headless는 모든 단계를 **프롬프트로 설명**하고, Claude Code가 자율적으로 실행
- 따라서 CC Headless 엔진은 코드가 훨씬 간단하지만, 동작이 덜 예측 가능함

---

## 8. MCP 서버 — 외부 데이터 수집 도구

MCP(Model Context Protocol)는 AI 에이전트가 외부 서비스의 데이터를 조회할 수 있게 해주는 프로토콜입니다.

```mermaid
graph TB
    subgraph Agent["RCA 에이전트"]
        AI["AI 모델<br/>(Sonnet / Haiku)"]
    end

    subgraph MCP["MCP 서버들"]
        subgraph AK["AWS Knowledge MCP"]
            AK1["search_documentation<br/>AWS 공식 문서 검색"]
            AK2["read_documentation<br/>특정 문서 읽기"]
            AK3["retrieve_agent_sops<br/>트러블슈팅 SOP 조회"]
        end

        subgraph CW["CloudWatch MCP"]
            CW1["get_metric_data<br/>메트릭 조회"]
            CW2["start_query<br/>Logs Insights 쿼리"]
            CW3["describe_alarms<br/>알람 상세 정보"]
        end

        subgraph CT["CloudTrail MCP"]
            CT1["lookup_events<br/>API 호출 이력 조회"]
        end
    end

    subgraph AWS["AWS 서비스"]
        CW_API["CloudWatch<br/>Metrics / Logs"]
        CT_API["CloudTrail<br/>Events"]
        AWS_DOCS["AWS 공식 문서"]
    end

    AI <--> AK
    AI <--> CW
    AI <--> CT

    AK --> AWS_DOCS
    CW --> CW_API
    CT --> CT_API

    style AK fill:#e8f5e9,stroke:#388e3c
    style CW fill:#e3f2fd,stroke:#1565c0
    style CT fill:#fff3e0,stroke:#ef6c00
```

### MCP 서버 설치 방식

| MCP 서버 | 실행 방식 | 비고 |
|----------|----------|------|
| AWS Knowledge | `uvx fastmcp run https://...` (stdio) | AWS 공식 문서/SOP 검색 |
| CloudWatch | `uvx --from awslabs-cloudwatch-mcp-server awslabs.cloudwatch-mcp-server` (stdio) | 메트릭·로그 조회 |
| CloudTrail | `uvx --from awslabs-cloudtrail-mcp-server awslabs.cloudtrail-mcp-server` (stdio) | 배포·변경 이력 |
| GitHub | `github-mcp-server stdio` (Go 바이너리, 컨테이너 내장) | 커밋 diff·PR 조회 |

AWS 관련 MCP 서버는 `uvx` (Python 패키지 런처)로 실행됩니다. GitHub MCP 서버는 Go 바이너리로, Docker 이미지 빌드 시 GitHub Releases에서 다운로드하여 포함합니다.

---

## 9. Healthcare Sensor App — 데모용 서비스

RCA 에이전트의 정확도를 검증하기 위한 **의도적으로 장애를 주입할 수 있는** 데모 서비스입니다.

```mermaid
graph TB
    subgraph HealthApp["Healthcare Sensor App (ECS Fargate)"]
        subgraph API["FastAPI 엔드포인트"]
            SENSOR["/sensors/data<br/>센서 데이터 수집"]
            PATIENT["/patients/{id}/vitals<br/>환자 바이탈 조회"]
            ALERT["/alerts<br/>이상 징후 알림"]
            HEALTH["/healthz<br/>헬스체크"]
        end

        subgraph FaultAPI["장애 주입 API"]
            DB_LEAK["/fault/db-leak<br/>DB 커넥션 누수"]
            HIGH_CPU["/fault/high-cpu<br/>CPU 과부하"]
            HIGH_MEM["/fault/high-memory<br/>메모리 과부하"]
            SLOW_Q["/fault/slow-query<br/>느린 쿼리"]
        end

        TRAFFIC["Background Traffic Generator<br/>10명 가상 환자<br/>5초 간격 데이터 생성<br/>92% 정상 + 8% 이상"]
    end

    subgraph Infra["인프라"]
        RDS["PostgreSQL 17.4<br/>(RDS)"]
        CW2["CloudWatch<br/>(메트릭 자동 수집)"]
        OTEL["ADOT Collector<br/>(OpenTelemetry)"]
    end

    API --> RDS
    FaultAPI --> RDS
    TRAFFIC --> API
    HealthApp --> OTEL --> CW2

    style FaultAPI fill:#fce4ec,stroke:#c62828
    style TRAFFIC fill:#e8f5e9,stroke:#388e3c
```

### 장애 주입 API 목록

| 엔드포인트 | 동작 | CloudWatch 알람 트리거 |
|-----------|------|----------------------|
| `POST /fault/db-leak` | DB 커넥션을 열고 닫지 않음 | RDS DatabaseConnections 급증 |
| `POST /fault/db-leak/reset` | 누수된 커넥션 정리 | — |
| `POST /fault/high-cpu` | CPU 집중 작업 실행 | ECS CPUUtilization 급증 |
| `POST /fault/high-cpu/reset` | CPU 부하 중지 | — |
| `POST /fault/high-memory` | 메모리 대량 할당 | ECS MemoryUtilization 급증 |
| `POST /fault/high-memory/reset` | 할당 메모리 해제 | — |
| `POST /fault/slow-query` | 의도적으로 느린 쿼리 실행 | RDS ReadLatency 급증 |
| `POST /fault/slow-query/reset` | 느린 쿼리 중지 | — |

> **참고**: `high-cpu`와 `slow-query` 장애는 명시적으로 reset 호출할 때까지 지속됩니다.

> **Cloud Map DNS**: VPC 내부에서 `healthcare.rcaagentdev.local`로 접근할 수 있습니다.

### RCA 대시보드

`packages/dashboard`에 Nuxt.js 기반 로컬 전용 대시보드가 있습니다. DynamoDB 세션 목록과 S3 보고서를 조회할 수 있습니다.

```bash
cd packages/dashboard
pnpm dev   # http://localhost:3100
```

---

## 10. 데모 시나리오 1: DB 커넥션 누수

가장 대표적인 데모 시나리오입니다. DB 커넥션을 누수시켜 장애를 발생시키고, RCA 에이전트가 이를 자동 분석합니다.

### 전체 흐름

```mermaid
sequenceDiagram
    participant OPS as 👩‍💻 운영자
    participant APP as Healthcare App
    participant CW as CloudWatch
    participant RCA as RCA Agent
    participant MCP as MCP 서버들
    participant AI as Amazon Bedrock
    participant SRE as SRE 팀

    Note over OPS,SRE: Phase 1: 장애 주입
    OPS->>APP: POST /fault/db-leak {"count": 50}
    APP->>APP: DB 커넥션 50개 열고 반환하지 않음
    Note over APP: 커넥션 풀 고갈 시작

    Note over APP,CW: Phase 2: 알람 발생
    APP-->>CW: DatabaseConnections 메트릭 상승
    CW->>CW: 알람 임계치 초과 감지
    CW->>RCA: 알람 → SNS → SQS → RCA 에이전트

    Note over RCA,AI: Phase 3: 자동 RCA 분석
    RCA->>MCP: CloudWatch 메트릭 조회
    MCP-->>RCA: 커넥션 수 선형 증가 확인
    RCA->>AI: 가설 생성 요청
    AI-->>RCA: 3개 가설 (배포 결함 / 트래픽 급증 / RDS 이슈)

    RCA->>MCP: CloudTrail 배포 이력 조회
    MCP-->>RCA: 장애 2시간 전 ECS 배포 확인
    RCA->>MCP: CloudWatch 로그 검색
    MCP-->>RCA: "Too many connections" 에러 다수

    RCA->>AI: 가설 검증
    AI-->>RCA: 배포 결함 → CONFIRMED (0.92)

    Note over RCA,SRE: Phase 4: 결과 전달
    RCA->>RCA: 보고서 생성 + S3 저장
    RCA->>SRE: SNS 알림 (presigned URL)
    SRE->>SRE: 보고서 확인 + 조치

    Note over OPS,APP: Phase 5: 정리
    OPS->>APP: POST /fault/db-leak/reset
    APP->>APP: 누수 커넥션 정리
```

### RCA 에이전트가 실제로 수행하는 분석

```mermaid
graph TD
    subgraph Scoping["① 스코핑"]
        S1["DatabaseConnections 메트릭 조회 (30분)"]
        S2["Latency, ErrorRate 메트릭 조회"]
        S3["심각도 판정: HIGH<br/>영향 범위: 서비스 전체"]
    end

    subgraph Hypothesis["② 가설 생성"]
        H_A["가설 A: 최근 배포 코드 결함<br/>confidence: 0.7"]
        H_B["가설 B: 트래픽 급증<br/>confidence: 0.5"]
        H_C["가설 C: RDS 인스턴스 문제<br/>confidence: 0.4"]
    end

    subgraph Evidence["③ 증거 수집"]
        E_A1["CloudTrail: 2시간 전 ECS 배포 확인 ✅"]
        E_A2["CloudWatch: 배포 시점부터 커넥션 선형 증가 ✅"]
        E_A3["CloudWatch Logs: Too many connections ✅"]
        E_B1["CloudWatch: RequestCount 평소 수준 ❌"]
        E_C1["CloudWatch: FreeStorageSpace/CPU 정상 ❌"]
    end

    subgraph Validation["④ 검증 결과"]
        V_A["가설 A: NEEDS_INVESTIGATION (0.75)<br/>배포 관련이지만 구체적 결함 미확인"]
        V_B["가설 B: REJECTED (0.1)"]
        V_C["가설 C: REJECTED (0.15)"]
    end

    subgraph Branch["⑤ 분기 (가설 A 세분화)"]
        B1["A-1: 커넥션 풀 설정 변경"]
        B2["A-2: 코드에서 커넥션 미반환 (누수)"]
    end

    subgraph Final["⑥ 최종 검증"]
        F1["A-1: REJECTED (풀 설정 변경 없음)"]
        F2["A-2: CONFIRMED ✅ (confidence: 0.92)<br/>배포 시점 + 선형 증가 + 에러 패턴 일치"]
    end

    Scoping --> Hypothesis --> Evidence --> Validation --> Branch --> Final

    style Final fill:#e8f5e9,stroke:#388e3c
```

---

## 11. 데모 시나리오 2: CPU 과부하

```mermaid
sequenceDiagram
    participant OPS as 👩‍💻 운영자
    participant APP as Healthcare App
    participant CW as CloudWatch
    participant RCA as RCA Agent

    OPS->>APP: POST /fault/high-cpu {"seconds": 300}
    APP->>APP: 5분간 CPU 집중 작업 실행
    APP-->>CW: ECS CPUUtilization > 80%
    CW->>RCA: 알람 발생

    Note over RCA: RCA 분석 수행
    RCA->>RCA: 스코핑: CPU 급증 감지
    RCA->>RCA: 가설: 배포/무한루프/외부요청 급증
    RCA->>RCA: 증거: 배포 없음, 트래픽 정상
    RCA->>RCA: 결론: 특정 프로세스의 CPU 과사용
    RCA->>RCA: 보고서 + 알림 전송
```

이 시나리오에서 RCA 에이전트는:
- CloudWatch 메트릭에서 CPU 급증 시점 확인
- CloudTrail에서 최근 배포/변경 없음 확인
- 트래픽 패턴 정상 확인
- "특정 프로세스의 비정상적 CPU 사용" 으로 결론

---

## 12. 데모 시나리오 3: Slow Query

```mermaid
sequenceDiagram
    participant OPS as 👩‍💻 운영자
    participant APP as Healthcare App
    participant CW as CloudWatch
    participant RCA as RCA Agent

    OPS->>APP: POST /fault/slow-query {"seconds": 30}
    APP->>APP: 30초짜리 느린 쿼리 실행
    APP-->>CW: RDS ReadLatency 급증
    CW->>RCA: 알람 발생

    Note over RCA: RCA 분석 수행
    RCA->>RCA: 스코핑: ReadLatency 급증 감지
    RCA->>RCA: 가설: 잠금충돌/인덱스누락/대량쿼리
    RCA->>RCA: 증거: 특정 시점에 장시간 쿼리 실행 확인
    RCA->>RCA: 결론: 비효율적 쿼리로 인한 지연
    RCA->>RCA: 보고서 + 알림 전송
```

---

## 13. 세션 상태와 DynamoDB

RCA 세션의 생명주기를 DynamoDB에서 추적합니다. 두 엔진 모두 같은 테이블을 공유합니다.

### Fargate 세션 상태 전이

```mermaid
stateDiagram-v2
    [*] --> ALARM_RECEIVED: SQS 메시지 수신

    ALARM_RECEIVED --> SCOPING: 파싱 완료
    SCOPING --> HYPOTHESIS_GENERATION: 스코핑 완료
    HYPOTHESIS_GENERATION --> HYPOTHESIS_PRIORITIZATION: 가설 생성
    HYPOTHESIS_PRIORITIZATION --> EVIDENCE_COLLECTION: 우선순위 결정
    EVIDENCE_COLLECTION --> HYPOTHESIS_VALIDATION: 증거 수집 완료
    HYPOTHESIS_VALIDATION --> REPORT_GENERATION: 종료 조건 충족
    HYPOTHESIS_VALIDATION --> HYPOTHESIS_PRIORITIZATION: 분기 후 재루프
    REPORT_GENERATION --> REMEDIATION: 보고서 생성 완료
    REMEDIATION --> VERIFICATION: 복구 실행
    VERIFICATION --> COMPLETED: 검증 완료 + 알림

    ALARM_RECEIVED --> FAILED: 오류
    SCOPING --> FAILED: 오류
    EVIDENCE_COLLECTION --> FAILED: 오류
    REPORT_GENERATION --> FAILED: 오류

    COMPLETED --> [*]
    FAILED --> [*]
```

### CC Headless 세션 상태 전이

```mermaid
stateDiagram-v2
    [*] --> ALARM_RECEIVED: SQS Event Source

    ALARM_RECEIVED --> ANALYZING: 세션 생성
    ALARM_RECEIVED --> [*]: 중복 감지 → 스킵

    ANALYZING --> COMPLETED: CC 성공 → 보고서 + 알림
    ANALYZING --> FAILED: CC 오류 / 타임아웃

    COMPLETED --> [*]
    FAILED --> [*]
```

### DynamoDB 테이블 구조

| 키 | 설명 | 예시 |
|----|------|------|
| `PK` (Partition Key) | `RCA#{rca_id}` | `RCA#a1b2c3d4-...` |
| `SK` (Sort Key) | `SESSION` | `SESSION` |
| `engine` | 실행 엔진 구분 | `strands` 또는 `cc-headless` |
| `state` | 현재 상태 | `ANALYZING`, `COMPLETED`, `FAILED` |
| `alarm_name` | 알람 이름 | `HighDatabaseConnections` |
| `idempotency_key` | 중복 방지 키 | `AlarmName#2026-04-23T10:00:00Z` |
| `ttl` | 자동 삭제 시간 | 30일 후 |

---

## 14. 장애 대응 체크리스트

RCA 에이전트 시스템 자체에 문제가 생겼을 때 확인할 사항입니다.

### RCA가 동작하지 않을 때

```mermaid
flowchart TD
    START["RCA 알림이 오지 않음"] --> Q1{"CloudWatch 알람이<br/>발생했는가?"}
    Q1 -->|"No"| A1["알람 설정 확인<br/>CloudWatch > Alarms"]
    Q1 -->|"Yes"| Q2{"SNS 메시지가<br/>전달되었는가?"}
    Q2 -->|"No"| A2["SNS Topic 구독 확인<br/>SNS > Subscriptions"]
    Q2 -->|"Yes"| Q3{"SQS에 메시지가<br/>도착했는가?"}
    Q3 -->|"No"| A3["SQS Queue 설정 확인<br/>SQS > Queue details"]
    Q3 -->|"Yes"| Q4{"DynamoDB에<br/>세션이 생성되었는가?"}
    Q4 -->|"No"| A4["Fargate 로그 확인<br/>CloudWatch > Log groups"]
    Q4 -->|"Yes"| Q5{"세션 상태가<br/>FAILED인가?"}
    Q5 -->|"No"| A5["아직 처리 중 — 대기<br/>(Strands: 최대 20분<br/>CC Headless: 제한 없음)"]
    Q5 -->|"Yes"| A6["에러 원인 확인<br/>DDB의 error 필드 확인<br/>CloudWatch Logs 검색"]
```

### 확인할 CloudWatch Log Groups

| 서비스 | Log Group | 확인 사항 |
|--------|-----------|----------|
| Fargate RCA Agent | `/ecs/rca-agent-*` | MCP 연결 실패, Bedrock API 오류 |
| Fargate CC Headless | `/ecs/*/cc-headless` | CC CLI 오류 |
| Healthcare App | `/ecs/healthcare-*` | 장애 주입 동작, 트래픽 생성기 |
| SQS DLQ | DLQ 메시지 수 | 처리 실패한 알람 메시지 |

### 일반적인 문제와 해결책

| 증상 | 원인 | 해결 |
|------|------|------|
| MCP 서버 연결 실패 | uvx 패키지 다운로드 실패 | NAT Gateway/인터넷 연결 확인 |
| CC CLI 0초 완료 | HOME 디렉토리 미설정 | 컨테이너 환경변수 HOME=/tmp 확인 |
| 중복 RCA 실행 | 멱등성 키 불일치 | DynamoDB GSI `idempotency-index` 확인 |
| Bedrock API 오류 | 리전/모델 설정 오류 | BEDROCK_REGION, MODEL_ID 환경변수 확인 |
| 보고서 S3 업로드 실패 | IAM 권한 부족 | Task Role의 S3 PutObject 권한 확인 |

---

## 15. 부록: 데모 실행 가이드

### 데모 실행 순서

```mermaid
flowchart LR
    A["1️⃣ Healthcare App<br/>정상 동작 확인"] --> B["2️⃣ 장애 주입<br/>(Fault API 호출)"]
    B --> C["3️⃣ CloudWatch 알람<br/>발생 대기 (2~5분)"]
    C --> D["4️⃣ RCA 분석 자동 시작<br/>(DynamoDB 세션 확인)"]
    D --> E["5️⃣ SNS 알림 수신<br/>(보고서 URL 확인)"]
    E --> F["6️⃣ 장애 정리<br/>(Reset API 호출)"]
```

### DB 커넥션 누수 데모 실행

```bash
# 1. Healthcare App 엔드포인트 확인 (ECS Service 주소)
HEALTH_URL="http://<healthcare-service-endpoint>:8000"

# 2. 헬스체크 확인
curl $HEALTH_URL/healthz

# 3. 장애 주입: DB 커넥션 50개 누수
curl -X POST $HEALTH_URL/fault/db-leak \
  -H "Content-Type: application/json" \
  -d '{"count": 50}'

# 4. CloudWatch 알람 발생 대기 (2~5분)
# → RCA 에이전트가 자동으로 분석 시작

# 5. DynamoDB에서 세션 상태 확인
aws dynamodb scan \
  --table-name <RCA-SESSION-TABLE> \
  --filter-expression "#s = :state" \
  --expression-attribute-names '{"#s": "state"}' \
  --expression-attribute-values '{":state": {"S": "COMPLETED"}}' \
  --query 'Items[0].{rca_id:rca_id.S,state:state.S,engine:engine.S}'

# 6. 장애 정리
curl -X POST $HEALTH_URL/fault/db-leak/reset
```

### CPU 과부하 데모 실행

```bash
# CPU 부하 생성 (reset 호출 전까지 지속)
curl -X POST $HEALTH_URL/fault/high-cpu

# 장애 정리
curl -X POST $HEALTH_URL/fault/high-cpu/reset
```

### Slow Query 데모 실행

```bash
# 30초 간격 느린 쿼리 반복 실행 (reset 호출 전까지 지속)
curl -X POST $HEALTH_URL/fault/slow-query \
  -H "Content-Type: application/json" \
  -d '{"seconds": 30}'

# 장애 정리
curl -X POST $HEALTH_URL/fault/slow-query/reset
```

### RCA 보고서 확인

RCA 완료 후 SNS 알림에 포함된 S3 presigned URL로 보고서를 확인할 수 있습니다. 또는 S3에서 직접 조회합니다:

```bash
# S3에서 보고서 목록 확인
aws s3 ls s3://<REPORT-BUCKET>/reports/ --recursive

# 최신 보고서 다운로드
aws s3 cp s3://<REPORT-BUCKET>/reports/<rca_id>.md ./rca-report.md
cat rca-report.md
```
