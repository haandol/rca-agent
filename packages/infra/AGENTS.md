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
├── EcrStack                      # ECR 리포지토리 (Codex 이미지는 기존 cc-headless 물리 이름 유지)
├── NetworkStack                  # VPC (Public + Private subnets, NAT Gateway)
├── EventBusStack                 # SNS Alarm Topic + SQS Queue (Fargate용) + DLQ
├── DatabaseStack                 # DynamoDB RCA 세션 테이블
├── StorageStack                  # S3 Evidence/Report 버킷
├── RdsStack                      # PostgreSQL 17.4 (Healthcare 서비스용)
├── RcaAgentServiceStack          # ECS Fargate — Strands RCA 에이전트
├── HeadlessCodexStack            # ECS Fargate — Headless Codex RCA 에이전트
├── HealthcareServiceStack        # ECS Fargate — Healthcare 센서 서비스 + Cloud Map DNS
└── PlaybookExecutionStack        # ECS Fargate — 승인된 플레이북 실행 워커 (실행 요청 큐 + 유일한 쓰기 권한 역할)
```

### Stack Dependencies

```
EcrStack ─────────────┐
NetworkStack ─────────┤
EventBusStack ────────┼── RcaAgentServiceStack
DatabaseStack ────────┤
StorageStack ─────────┘

EcrStack ─────────────┐
NetworkStack ─────────┤
EventBusStack ────────┼── HeadlessCodexStack
DatabaseStack ────────┤
StorageStack ─────────┘

EcrStack ─────────────┐
NetworkStack ─────────┼── HealthcareServiceStack
RdsStack ─────────────┘

NetworkStack ──────── RdsStack

EcrStack ─────────────┐
NetworkStack ─────────┼── PlaybookExecutionStack
DatabaseStack ────────┤
StorageStack ─────────┘
```

`PlaybookExecutionStack`은 `EventBusStack`에 의존하지 않는다. 실행은 알람이나 분석 완료
이벤트가 아니라 대시보드가 발행한 승인 요청으로만 시작되므로, 이 스택은 자신의 요청 큐만
가지며 어떤 토픽도 구독하지 않는다. 승인 없이 실행이 기동될 경로를 인프라에 두지 않는 것이
이 구조의 목적이다.

`HealthcareServiceStack`에도 명시적 의존을 두지 않는다. 실행 스택이 Healthcare 서비스의
SG 인그레스를 여는 순간 Healthcare 스택이 실행 스택을 참조하므로, 반대 방향 의존을 추가하면
순환 참조가 된다.

## Configuration

`packages/infra/.toml` 파일에서 환경별 설정을 관리한다.

| 섹션 | 키 | 설명 |
|------|-----|------|
| `app` | `ns`, `stage` | 네임스페이스, 스테이지 (리소스명 접두사) |
| `aws` | `region` | 배포 리전 |
| `alarm` | `notificationEmail` | SNS 알림 이메일 |
| `agent` | `imageTag` | RCA Agent ECS 이미지 태그 |
| `healthcare` | `imageTag` | Healthcare ECS 이미지 태그 |
| `headlessCodex` | `imageTag` | Headless Codex ECS 이미지 태그 |
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

### Headless Codex (Fargate Task Role)

- `CloudWatchReadOnlyAccess` (매니지드 정책)
- `AWSCloudTrail_ReadOnlyAccess` (매니지드 정책)
- SQS: ConsumeMessages
- DynamoDB: ReadWriteData
- S3: Evidence ReadWrite, Report PutObject/GetObject
- S3 Vectors: 전체 CRUD
- Bedrock: InvokeModel / InvokeModelWithResponseStream
- SNS: Publish (알림 토픽)

분석 태스크 역할에는 **쓰기 권한이 없고 Healthcare 서비스로의 네트워크 경로도 없다.**
복구는 사용자 승인 뒤 실행 스택이 수행하므로, 분석이 조사 대상을 변경할 수 있는 경로를
남기지 않는다.

### Playbook Execution (Fargate Task Role)

시스템에서 **쓰기 권한을 가진 유일한 태스크 역할**이다. 분석 엔진과 역할을 공유하지 않는다 —
공유하면 분석 경로의 결함이 쓰기 권한에 닿는다.

- SQS: ConsumeMessages (실행 요청 큐)
- DynamoDB: RCA 세션 테이블 ReadWriteData (실행 항목·상태·회고 결과)
- S3: Evidence ReadWrite (실행 증거, 갱신 전 플레이북 사본)
- S3 Vectors: 회고가 갱신한 플레이북 재인덱싱
- Bedrock: InvokeModel / InvokeModelWithResponseStream
- CloudWatchReadOnlyAccess (절차의 성공 판정 기준 관측)
- `PowerUserAccess` + 명시적 Deny: organizations, account, billing, budgets, ce, cur,
  aws-portal, sso, identitystore, IAM 변경
- SNS Publish 권한 없음 — 알림은 분석 완료를 알리는 신호이고, 실행이 분석 파이프라인을
  대변해서는 안 된다

**대상 리소스는 제한하지 않는다.** ARN으로 제한하면 플레이북이 기술할 수 있는 절차가 다시
허용 목록에 갇히므로, 실행 근거를 플레이북으로 옮긴 결정과 모순된다. Deny 목록은 애초에 실행
대상이 아닌 범위만 잘라낸다.

**파괴적 액션 차단은 이 역할이 아니라 실행 도구가 수행한다.** 작업 이름 어휘와 IAM 액션
이름이 일대일로 대응하지 않아 정책으로 표현하면 누락이 생기고, 정책 거부는 차단 사유를 실행
증거에 남기거나 해당 절차를 수동 조치로 표시할 수 없다. 도구는 둘 다 할 수 있다.

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
