# Deployment & Testing

## Infrastructure (CDK)

```bash
cd packages/infra
pnpm nx deploy infra          # 전체 스택 배포
pnpm cdk diff                 # 변경사항 확인
pnpm cdk deploy <StackName>   # 특정 스택 배포
```

### CDK Stacks (10개)

| Stack | Description |
|-------|-------------|
| EcrStack | ECR 리포지토리 (rca-agent, healthcare, cc-headless) |
| NetworkStack | VPC (Public + Private subnets, NAT Gateway) |
| EventBusStack | SNS Alarm Topic + SQS Queue (Fargate용) + DLQ |
| DatabaseStack | DynamoDB RCA 세션 테이블 |
| StorageStack | S3 Evidence/Report 버킷 + S3 Vectors (플레이북/보고서 임베딩) |
| RdsStack | PostgreSQL 17.4 (Healthcare 서비스용) |
| HealthcareServiceStack | ECS Fargate — Healthcare 센서 서비스 + Cloud Map Private DNS |
| RcaAgentServiceStack | ECS Fargate — Strands RCA 에이전트 (읽기 전용) |
| CcHeadlessStack | ECS Fargate — CC headless RCA 에이전트 (읽기 전용) |
| PlaybookExecutionStack | ECS Fargate — 사용자 승인 기반 플레이북 실행 워커 + 실행 요청 큐/DLQ (desiredCount 1) |

모든 서비스는 Private subnet에 배포되며, 인바운드 트래픽이 차단됩니다. 자세한 스택 의존관계와 IAM 권한은 [`packages/infra/AGENTS.md`](../packages/infra/AGENTS.md)를 참조하세요.

### 실행 스택의 경계

`PlaybookExecutionStack`은 시스템에서 **쓰기 권한을 가진 유일한 태스크 역할**입니다. 분석 스택과 역할을 공유하지 않는데, 공유하면 분석 경로의 결함이 쓰기 권한에 닿기 때문입니다.

- **이벤트 구독이 없습니다.** 이 스택으로 들어오는 유일한 경로는 대시보드가 실행 요청 큐에 발행하는 메시지입니다. 승인이 곧 메시지이므로, 사람이 승인하지 않고 실행이 기동될 경로가 인프라에 존재하지 않습니다.
- **대상 리소스를 ARN으로 제한하지 않습니다.** 제한하면 플레이북의 표현력을 다시 허용 목록 안으로 되돌리게 되므로, `PowerUserAccess`에 실행 대상이 될 수 없는 범위(organizations, account, billing, budgets, ce, cur, aws-portal, sso, identitystore, IAM 변경)를 명시적으로 Deny로 덧붙입니다.
- **파괴적 조치 차단은 IAM이 아니라 실행 도구가 수행합니다.** 작업 이름 어휘와 IAM 액션 이름이 일대일로 대응하지 않아 정책으로는 빈틈이 생기고, 정책 거부는 어떤 절차가 왜 막혔는지 기록하거나 그 절차를 수동 조치로 남길 수 없습니다.
- **요청 큐의 visibility timeout이 최악 실행 시간을 넘습니다** (실행 상한 3600초, visibility 4500초). 그렇지 않으면 실행 중인 요청이 재전달되어 두 번 실행됩니다.
- 상시 1개 태스크로 운영합니다. 태스크 수로 기능을 여닫는 것은 트리거가 이벤트 구독이던 시절의 장치이며, 승인 게이트가 있으면 큐가 이미 실행 여부를 결정합니다.

실행 워커의 이미지 태그는 `config/dev.toml`의 `[execution]` 섹션(`imageTag`) 또는 `EXECUTION_IMAGE_TAG` 환경변수로 지정합니다.

### 서비스 단위 배포

```bash
pnpm --filter infra run deploy:service -- cc-headless execution   # 같은 이미지, 두 진입점
pnpm --filter infra run deploy:service -- --list
pnpm --filter infra run deploy:service -- --status execution
```

이미지 태그는 현재 커밋 SHA로 고정됩니다. `latest`는 가변 태그라서 배포된 하네스 버전을 태그만으로 식별할 수 없으므로 배포 대상으로 쓰지 않습니다. 커밋되지 않은 변경이 있으면 태그에 `-dirty`가 붙어 재현 불가 상태가 드러납니다.

**`config/*.toml`에는 `imageTag` 기본값을 두지 않습니다.** 기본값이 있으면 태그를 주입하지 않은 배포가 조용히 그 값으로 되돌아가는데, CDK가 배포 대상이 **의존하는 스택까지 함께 갱신**하기 때문에 그 되돌림이 배포 대상이 아닌 서비스의 태스크 정의에까지 번집니다. 태그가 없으면 synth가 실패해 배포가 즉시 멈춥니다.

그래서 `cdk deploy`를 직접 호출할 때는 **네 서비스의 태그를 모두** 넘겨야 합니다. `deploy:service`는 배포 대상의 태그만 새로 지정하고 나머지는 현재 떠 있는 태스크 정의의 태그를 읽어 그대로 넘기므로, 이 스크립트를 쓰면 신경 쓸 필요가 없습니다.

분석 워커와 실행 워커는 같은 이미지를 공유하므로 두 서비스를 함께 배포해도 빌드·푸시는 한 번만 수행됩니다. 다만 **스택은 각각 배포해야** 합니다 — 태스크 정의가 불변 태그를 직접 가리켜야 하고, 두 워커의 진입점이 다르기 때문입니다.

## Agent — Fargate (Strands)

에이전트는 ECS Fargate 태스크로 배포됩니다. SQS 큐를 Long Polling으로 구독하며, 알람 메시지 수신 시 RCA 워크플로우를 자동 시작합니다.

## Agent — Fargate (CC Headless 분석 워커)

CC Headless 분석 에이전트는 ECS Fargate 태스크로 배포됩니다. SQS 알람 큐를 Long Polling으로 구독하며, CC CLI가 RCA → Report 전문 서브 에이전트를 순서대로 호출해 플레이북을 포함한 리포트 하나를 만들고 종료합니다. 진입점은 `python -m cc_headless.main`이며, 태스크 역할에 쓰기 권한이 없고 Healthcare 서비스로의 네트워크 경로도 없습니다.

```bash
cd packages/cc-headless
docker build -t cc-headless .
```

## Playbook Execution — Fargate (CC Headless 실행 워커)

실행 워커는 **분석 워커와 같은 컨테이너 이미지를 다른 진입점으로** 실행합니다: `python -m cc_headless.execution_main`. 하나의 하네스를 두 진입점으로 나눈 구성이라 이미지를 따로 빌드하지 않습니다.

- 실행 요청 큐를 Long Polling으로 구독합니다. 대시보드의 `POST /api/executions`가 발행한 승인 요청만 소비하며, 이벤트 구독이 없어 승인 없이 시작될 경로가 없습니다.
- 실행 근거는 리포트에 담긴 플레이북의 `execution_steps`(`step_id`, `intent`, `action`, `success_criteria`)입니다. `action`은 자연어 서술이고, 리소스 식별자와 리전은 실행 시점의 알람 컨텍스트에서 옵니다.
- 해결이 관측으로 확정된 실행(`RESOLVED`)에 한해 회고가 이어서 실행되며, 플레이북 절차를 교정합니다.
- 배포 시 `EXECUTION_QUEUE_URL`, `EXECUTION_TIMEOUT_SECONDS`(기본 3600) 환경변수가 스택에서 주입됩니다.

## Healthcare Sensor App

PostgreSQL + background traffic generator로 CloudWatch baseline 메트릭을 축적합니다. ECS Fargate로 배포되며, fault injection API로 장애 시나리오를 트리거합니다.

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
