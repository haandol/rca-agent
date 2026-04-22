# AGENTS.md

> 이 패키지는 RCA Agent 모노레포의 일부입니다. 전체 아키텍처, ADR, 크로스 패키지 계약, 빌드 명령어는 **[루트 AGENTS.md](../../AGENTS.md)** 를 참조하세요.

## Project Overview

AWS CDK (TypeScript) 기반 인프라 패키지. RCA Agent 시스템의 전체 AWS 인프라를 코드로 정의한다.

### Tech Stack

- **IaC**: AWS CDK v2 (TypeScript)
- **Package Manager**: pnpm (Nx workspace)
- **Lint**: ESLint + Prettier
- **Config**: TOML (`.toml`)

## Quick Start

```bash
# 의존성 설치
pnpm install

# 전체 스택 배포
pnpm cdk deploy "*" --require-approval never --concurrency 4

# 특정 스택 배포
pnpm cdk deploy RcaAgentDevRcaAgentServiceStack

# 변경사항 확인
pnpm cdk diff

# 빌드 & 린트
pnpm build
pnpm lint
```

## CDK Stack Architecture

```
RcaAgentDev
├── EcrStack                      # ECR 리포지토리 (rca-agent, healthcare, cc-headless)
├── NetworkStack                  # VPC (Public + Private subnets, NAT Gateway)
├── EventBusStack                 # SNS Alarm Topic + SQS Queue (Fargate용) + DLQ
├── DatabaseStack                 # DynamoDB RCA 세션 테이블
├── StorageStack                  # S3 Evidence/Report 버킷
├── RdsStack                      # PostgreSQL 17.4 (Healthcare 서비스용)
├── RcaAgentServiceStack          # ECS Fargate — Strands RCA 에이전트
├── CcHeadlessStack               # Lambda Container — CC headless RCA 에이전트
└── HealthcareServiceStack        # ECS Fargate — Healthcare 센서 서비스
```

### Stack Dependencies

```
EcrStack ─────────────┐
NetworkStack ─────────┤
EventBusStack ────────┼── RcaAgentServiceStack
DatabaseStack ────────┤
StorageStack ─────────┘

EcrStack ─────────────┐
EventBusStack ────────┼── CcHeadlessStack
DatabaseStack ────────┤
StorageStack ─────────┘

EcrStack ─────────────┐
NetworkStack ─────────┼── HealthcareServiceStack
RdsStack ─────────────┘

NetworkStack ──────── RdsStack
```

## Configuration

`packages/infra/.toml` 파일에서 환경별 설정을 관리한다.

| 섹션 | 키 | 설명 |
|------|-----|------|
| `app` | `ns`, `stage` | 네임스페이스, 스테이지 (리소스명 접두사) |
| `aws` | `region` | 배포 리전 |
| `alarm` | `notificationEmail` | SNS 알림 이메일 |
| `agent` | `imageTag` | RCA Agent ECS 이미지 태그 |
| `healthcare` | `imageTag` | Healthcare ECS 이미지 태그 |
| `ccHeadless` | `imageTag` | CC Headless Lambda 이미지 태그 |
| `storage` | `evidenceBucket`, `vectorBucket` | S3 버킷명 |
| `table.rcaSession` | `name` | DynamoDB 테이블명 |
| `tracing` | `enabled` | OpenTelemetry 사이드카 활성화 |

## IAM Permissions

### RCA Agent (Fargate Task Role)

- `CloudWatchReadOnlyAccess` (매니지드 정책)
- `AWSCloudTrail_ReadOnlyAccess` (매니지드 정책)
- SQS: ConsumeMessages
- DynamoDB: ReadWriteData
- S3: Evidence/Report ReadWrite
- S3 Vectors: 전체 CRUD
- Bedrock: InvokeModel / InvokeModelWithResponseStream
- X-Ray: BatchGetTraces, GetTraceSummaries, PutTraceSegments, PutTelemetryRecords
- SNS: Publish (알림 토픽)

### CC Headless (Lambda Execution Role)

- `CloudWatchReadOnlyAccess` (매니지드 정책)
- `AWSCloudTrail_ReadOnlyAccess` (매니지드 정책)
- DynamoDB: ReadWriteData
- S3: Evidence ReadWrite, Report PutObject/GetObject
- S3 Vectors: 전체 CRUD
- Bedrock: InvokeModel / InvokeModelWithResponseStream
- SNS: Publish (알림 토픽)

### Healthcare (Fargate Task Role)

- ECR Pull (매니지드 정책)
- X-Ray: PutTraceSegments, PutTelemetryRecords (tracing 활성화 시)

## Agent Guidelines

### Safe to Modify

- Stack 파일 (`lib/stacks/`)
- Config (`config/loader.ts`, `.toml`)

### Approach with Caution

- `bin/infra.ts` — CDK 앱 엔트리포인트 (스택 간 의존성 정의)
- Stack 간 cross-reference (DependencyCycle 주의)

### Common Mistakes to Avoid

- `fromRegistry()` 사용 시 ECR Pull 권한 자동 부여 안 됨 — 명시적 `AmazonEC2ContainerRegistryReadOnly` 매니지드 정책 필요
- Cross-stack Security Group 참조로 DependencyCycle 발생 — VPC CIDR 기반 인바운드 규칙 사용
- 환경변수명 불일치 — CDK에서 설정한 이름과 애플리케이션 코드가 읽는 이름이 반드시 일치해야 함
