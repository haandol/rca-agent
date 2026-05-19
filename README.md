# RCA Agent — AWS 기반 자동 RCA 분석 에이전트

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

AWS 환경에서 CloudWatch 알람 발생 시 자동 RCA(근본원인분석)를 수행하는 closed-loop 에이전트 시스템입니다. 두 가지 실행 엔진(Strands Agents SDK 9단계 파이프라인 / CC on Bedrock headless 프롬프트 주도)을 지원하며, MCP 서버를 통해 CloudWatch, CloudTrail, GitHub 데이터 소스를 자동 분석합니다.

## 패키지 구성

| Package | Description | Tech |
|---------|-------------|------|
| [`packages/agent`](./packages/agent/) | Strands Agents SDK 기반 RCA 에이전트 — 9단계 파이프라인 (단일 Sonnet 모델 + Planning/Execution 행동 분리) | Python, Strands Agents SDK, Amazon Bedrock |
| [`packages/cc-headless`](./packages/cc-headless/) | CC on Bedrock headless 기반 RCA 에이전트 — ECS Fargate에서 SQS Long Polling + CC CLI로 단일 프롬프트 RCA 수행 | Python, Claude Code CLI, ECS Fargate |
| [`packages/infra`](./packages/infra/) | AWS CDK 인프라 — ECS Fargate, SNS/SQS, S3, S3 Vectors, DynamoDB, VPC, Cloud Map | TypeScript, CDK |
| [`packages/healthcare-sensor-app`](./packages/healthcare-sensor-app/) | 헬스케어 센서 데이터 수집/조회 서비스 — 영구 지속형 장애 주입 + reset API, background traffic generator | Python, FastAPI, PostgreSQL |
| [`packages/dashboard`](./packages/dashboard/) | RCA 대시보드 — DynamoDB 세션 상태, S3 보고서/플레이북/증거 조회, 파이프라인 트레이스 그래프 (로컬 전용) | TypeScript, Nuxt.js 4, Vue Flow |

## 주요 기능

### Dual-Stack RCA 실행
- **Fargate Stack (Strands)**: CloudWatch Alarm → SNS → SQS → ECS Fargate, 9단계 closed-loop 파이프라인 (Scoping → Hypothesis → Prioritization → Beam Selection → Evidence → Validation → Branching → Report → Playbook → Notification)
- **Fargate Stack (CC Headless)**: CloudWatch Alarm → SNS → SQS → ECS Fargate, 단일 프롬프트 주도 RCA
- 동일 SNS 토픽을 독립 구독하여 A/B 비교 가능
- DynamoDB `engine` 필드로 실행 엔진 구분, `IDEMP#` 키로 멱등성 보장

### Strands Agent Stack
- 단일 모델(Sonnet 4.6) + Planning/Execution 행동 분리 (Planning은 adaptive thinking, Execution은 thinking 없음)
- 가설별 독립 Agent 인스턴스로 증거 수집 세션 격리 (컨텍스트 오버플로우 방지)
- 계층적 부모 요약 주입으로 하위 가설 증거 수집 컨텍스트 강화
- Beam Search 탐색: 우선순위 상위 N개(기본 3) 가설만 선택적 검증
- 신뢰도/시간/깊이/루프 기반 종료 조건으로 운영 통제
- 전체 기각 시 자동 가설 재생성 (최대 2회)
- 가설 상태: PENDING → CONFIRMED / REJECTED / CLOSED / NEEDS_INVESTIGATION
- 유사 보고서 검색: 스코핑 시 과거 RCA 보고서의 "증상 → 근본 원인" 경로를 활용하여 가설 생성 정확도 향상
- 플레이북 검색 우선(search-first) 전략: 유사 플레이북 업데이트 또는 신규 생성

### CC Headless Stack
- Claude Code CLI headless 모드 + Bedrock 백엔드 (ECS Fargate)
- 단일 프롬프트에 11단계 RCA 워크플로우 정의 (스코핑 ~ 검증 ~ 복구 ~ 보고서)
- MCP 서버 구성: AWS Knowledge, CloudWatch, CloudTrail, GitHub (`mcp-config.json`)
- CC가 MCP 도구를 자율적으로 호출

### 공통 — Hexagonal Architecture
- 양쪽 패키지(agent, cc-headless) 모두 Ports & Adapters 패턴 적용
- 비즈니스 로직(services/)은 Port 인터페이스(ports/interfaces/)에만 의존, 인프라 구체 클래스(adapters/)와 분리
- DI Container로 AWS Adapter(DynamoDB, S3, SNS, Bedrock)를 lazy-init 주입
- DTO(ports/dto/)를 공유 데이터 모델로 사용

### 공통
- AWS Knowledge + CloudWatch + CloudTrail + GitHub MCP 서버를 통한 데이터 자동 수집
- RCA 보고서 자동 생성 및 S3 저장
- S3 Vectors 기반 유사 보고서/플레이북 검색 (가설 생성 정확도 향상)
- SNS 알림 전송 (Presigned URL 보고서 링크 포함)
- DynamoDB 기반 파이프라인 실행 트레이스 (단계별 추적)

## 사전 요구사항

- Node.js 20+, pnpm
- Python 3.12+, [uv](https://docs.astral.sh/uv/)
- AWS CLI (인증 설정 완료)
- [gh](https://cli.github.com/) (GitHub CLI)
- Docker (컨테이너 빌드 및 배포용)

## 설치

```bash
# 모노레포 의존성 설치
pnpm install

# 전체 빌드
pnpm nx run-many -t build

# 전체 테스트
pnpm nx run-many -t test

# 전체 린트
pnpm nx run-many -t lint
```

### 패키지별 설치

```bash
# Agent (Python)
cd packages/agent
uv sync --extra dev

# CC Headless (Python)
cd packages/cc-headless
uv sync --extra dev

# Healthcare Sensor App (Python)
cd packages/healthcare-sensor-app
uv sync --extra dev

# Infra (TypeScript CDK)
cd packages/infra
pnpm install

# Dashboard (TypeScript Nuxt.js)
cd packages/dashboard
pnpm install
```

## 시크릿 설정

GitHub PAT(Personal Access Token)를 GitHub repo secret과 AWS Secrets Manager에 동시에 등록하는 스크립트를 제공합니다. 등록된 토큰은 CDK 배포 시 ECS task에 `GITHUB_PERSONAL_ACCESS_TOKEN` 환경변수로 자동 주입됩니다.

### GitHub PAT 발급

1. GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens
2. Repository access: 대상 레포지토리 선택
3. Permissions: 필요한 read 권한 부여
4. 토큰 생성 후 복사

### 시크릿 등록

```bash
# 기본 설정 (NAMESPACE=RcaAgentDev, AWS_REGION=us-east-1)
./packages/infra/scripts/setup-github-secrets.sh

# 커스텀 설정
NAMESPACE=RcaAgentProd AWS_REGION=ap-northeast-2 ./packages/infra/scripts/setup-github-secrets.sh
```

이 스크립트는 다음 두 곳에 토큰을 등록합니다:
- **GitHub repo secret**: `GH_PAT` (CI/CD용)
- **AWS Secrets Manager**: `{NAMESPACE}/github/pat` (ECS task 런타임용)

## 인프라 배포

### 설정

인프라 설정은 `packages/infra/.toml`에서 관리합니다.

```toml
[app]
ns = "RcaAgent"
stage = "Dev"

[aws]
region = "us-east-1"

[alarm]
notificationEmail = "your@email.com"

[agent]
imageTag = "latest"

[healthcare]
imageTag = "latest"

[ccHeadless]
imageTag = "latest"

[storage]
evidenceBucket = "rca-agent-dev-evidence"
vectorBucket = "rca-agent-dev-vectors"

[table.rcaSession]
name = "RcaAgentDevRcaSession"

[tracing]
enabled = true
```

### 배포

```bash
cd packages/infra

# CloudFormation 템플릿 합성 (검증용)
npx cdk synth

# 전체 스택 배포
npx cdk deploy --all

# 개별 스택 배포
npx cdk deploy RcaAgentDevRcaAgentServiceStack
npx cdk deploy RcaAgentDevCcHeadlessStack
npx cdk deploy RcaAgentDevHealthcareServiceStack
```

### 스택 구성 (9개)

| 스택 | 설명 |
|------|------|
| `EcrStack` | ECR 레포지토리 (rca-agent, cc-headless, healthcare) |
| `NetworkStack` | VPC, 서브넷, NAT Gateway |
| `EventBusStack` | SNS 토픽, SQS 큐 (알람 → 에이전트 연결) |
| `DatabaseStack` | DynamoDB 테이블 (RCA 세션) |
| `StorageStack` | S3 버킷 (증거, 보고서), S3 Vectors (플레이북/보고서 임베딩) |
| `RdsStack` | RDS PostgreSQL (Healthcare 서비스용) |
| `HealthcareServiceStack` | Healthcare 센서 앱 (ECS Fargate + CloudWatch 알람 + Cloud Map DNS) |
| `RcaAgentServiceStack` | Strands RCA 에이전트 (ECS Fargate) |
| `CcHeadlessStack` | CC Headless RCA 에이전트 (ECS Fargate) |

## 데모 시나리오: DB 커넥션 누수 장애

Healthcare 센서 서비스에 DB 커넥션 누수 장애를 주입하고, RCA 에이전트가 자동으로 근본 원인을 분석하는 전체 흐름입니다.

### 사전 조건

- 인프라 배포 완료 (`npx cdk deploy --all`)
- Healthcare 서비스 ECS 태스크 실행 중 (background traffic generator가 CloudWatch baseline 메트릭 축적)
- RCA 에이전트(Strands 또는 CC Headless) ECS 태스크 실행 중

### Step 1. 장애 주입

Healthcare 서비스의 fault injection API로 DB 커넥션 누수를 시작합니다. 이 API는 DB 세션을 열기만 하고 닫지 않아 커넥션이 점진적으로 누적됩니다.

```bash
# Healthcare 서비스의 Private DNS (Cloud Map)
# ECS 태스크에서 직접 호출하거나, VPN/Bastion 경유
HEALTHCARE_HOST="healthcare.rcaagentdev.local"

# DB 커넥션 누수 장애 주입
curl -X POST http://${HEALTHCARE_HOST}:8000/fault/db-leak
```

장애 주입 후 background traffic generator가 요청을 보내면서 커넥션이 누적됩니다. 수 분 내에 RDS `DatabaseConnections` 메트릭이 임계치를 초과하여 CloudWatch 알람이 발생합니다.

### Step 2. RCA 자동 실행

CloudWatch Alarm → SNS → SQS 경로로 알람이 전달되면, RCA 에이전트가 자동으로 분석을 시작합니다.

**Strands Agent (9단계 파이프라인)**:
1. **Scoping**: 알람 메트릭 + 유사 보고서 검색 (S3 Vectors)
2. **Hypothesis Generation**: 가설 3~5개 생성 (배포 코드 결함, 트래픽 급증, RDS 문제 등)
3. **Prioritization + Beam Selection**: 우선순위 결정, 상위 3개 선택
4. **Evidence Collection**: CloudWatch 메트릭/로그, CloudTrail 배포 이력, GitHub 코드 diff 수집
5. **Validation**: 가설별 검증 (CONFIRMED / REJECTED / NEEDS_INVESTIGATION)
6. **Branching**: 하위 가설 분기 (예: 코드 결함 → 커넥션 풀 설정 변경 / 커넥션 미반환)
7. **Report**: 근본 원인, 영향 범위, 조치 방안 포함 Markdown 보고서 생성
8. **Playbook**: 재사용 가능한 대응 플레이북 생성 및 S3 Vectors 인덱싱
9. **Notification**: SNS 알림 발행 (presigned URL + 플레이북 포함)

**CC Headless Agent (프롬프트 주도)**: 동일한 알람을 독립적으로 수신하여 단일 프롬프트로 전체 RCA를 자율 수행합니다.

### Step 3. 결과 확인

```bash
# 대시보드 실행 (로컬)
cd packages/dashboard
pnpm dev   # http://localhost:3100
```

대시보드에서 확인할 수 있는 항목:
- **세션 목록**: RCA 진행 상태 (COMPLETED / FAILED / ANALYSING 등)
- **트레이스 그래프**: Vue Flow DAG로 파이프라인 단계별 실행 결과 확인
- **보고서**: 근본 원인, 증거, 조치 방안이 포함된 Markdown 보고서
- **플레이북**: 재사용 가능한 대응 플레이북
- **증거 상세**: 가설별 full evidence (메트릭, 로그, 코드 diff 등)

### Step 4. 장애 해제

```bash
# DB 커넥션 누수 해제
curl -X POST http://${HEALTHCARE_HOST}:8000/fault/db-leak/reset
```

### 기타 장애 시나리오

Healthcare 서비스는 다음 장애 주입 API를 제공합니다:

| 엔드포인트 | 장애 유형 | 트리거되는 알람 |
|-----------|---------|--------------|
| `POST /fault/db-leak` | DB 커넥션 누수 | RDS DatabaseConnections 임계치 초과 |
| `POST /fault/high-cpu` | CPU 과부하 | ECS CPUUtilization 임계치 초과 |
| `POST /fault/high-memory` | 메모리 과부하 | ECS MemoryUtilization 임계치 초과 |
| `POST /fault/slow-query` | 슬로우 쿼리 | 응답 지연 증가 |

모든 장애는 `/reset` 엔드포인트로 해제됩니다 (예: `POST /fault/high-cpu/reset`).

## 환경 변수

### Strands Agent (packages/agent)

`python-dotenv`를 사용하여 `packages/agent/env/local.env`에서 설정을 로드합니다 (`override=False`, 기존 환경변수 우선).

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `BEDROCK_MODEL_ID` | `global.anthropic.claude-sonnet-4-6` | Planning/Execution 공용 모델 (단일 Sonnet 4.6) |
| `BEDROCK_MAX_TOKENS` | `16384` | 모델 최대 토큰 |
| `THINKING_ENABLED` | `false` | Planning 호출 시 adaptive thinking 피처플래그 |
| `SQS_QUEUE_URL` | - | SQS 큐 URL (필수) |
| `S3_VECTOR_BUCKET_NAME` | - | S3 Vectors 버킷 |
| `S3_REPORT_BUCKET` | - | 보고서 저장 S3 버킷 |
| `SNS_NOTIFICATION_TOPIC_ARN` | - | RCA 완료 알림 SNS 토픽 |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | - | GitHub MCP 인증 (Secrets Manager에서 주입) |

전체 환경변수 목록은 [`packages/agent/env/local.env`](./packages/agent/env/local.env)를 참조하세요. 자동 복구(Remediation) 활성화는 별도 에이전트의 desired count로 제어하며, 현재 설계 단계(ADR 0012)입니다.

### CC Headless (packages/cc-headless)

ECS Fargate 환경변수로 설정됩니다 (CDK 스택에서 자동 주입).

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `CLAUDE_CODE_USE_BEDROCK` | `1` | Bedrock 백엔드 활성화 |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `global.anthropic.claude-sonnet-4-6` | CC 기본 모델 |
| `DYNAMODB_TABLE_NAME` | - | 공유 RCA 세션 테이블 |
| `S3_EVIDENCE_BUCKET` | - | 공유 증거 버킷 |
| `S3_VECTOR_BUCKET_NAME` | - | 공유 S3 Vectors 버킷 |
| `S3_REPORT_BUCKET` | - | 공유 보고서 버킷 |
| `SNS_NOTIFICATION_TOPIC_ARN` | - | 알림 토픽 |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | - | GitHub MCP 인증 (Secrets Manager에서 주입) |

### Dashboard (packages/dashboard)

로컬 전용 대시보드로, 로컬 AWS 크레덴셜(`~/.aws`)을 사용합니다.

```bash
cd packages/dashboard
pnpm dev   # http://localhost:3100
```

## 테스트

```bash
# 전체 테스트
pnpm nx run-many -t test

# 특정 패키지 테스트
pnpm nx test agent
pnpm nx test infra

# 영향받은 프로젝트만 테스트
pnpm nx affected -t test

# Agent 단독 테스트
cd packages/agent
uv run pytest tests/
```

## 문서

| 문서 | 설명 |
|------|------|
| [PRD](./docs/prd/aws-rca-agent-prd.md) | 제품 요구사항 정의서 — 기능 명세, 데모 시나리오, KPI |
| [아키텍처 & 데모 플로우](./docs/architecture-and-demo-flow.md) | 데이터 플로우, 상태 전이, 데모 시나리오 머메이드 다이어그램 |
| [ADR Index](./docs/adr/README.md) | 아키텍처 결정 기록 인덱스 |
| [운영 가이드](./docs/system-guide-for-ops.md) | 주니어 DevOps 운영팀원을 위한 시스템 안내서 |
| [Contributing Guide](./CONTRIBUTING.md) | 커밋 메시지, 브랜치 전략, PR 규칙 |
| 패키지별 AGENTS.md | 각 패키지의 AGENTS.md에서 세부 기술 가이드 확인 |

## 기여하기

커밋 메시지, 브랜치 전략, PR 규칙 등 기여 규칙은 [CONTRIBUTING.md](./CONTRIBUTING.md)를 따릅니다.

## License

이 프로젝트는 [MIT License](./LICENSE) 하에 배포됩니다.
