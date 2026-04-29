# Deployment & Testing

## Infrastructure (CDK)

```bash
cd packages/infra
pnpm nx deploy infra          # 전체 스택 배포
pnpm cdk diff                 # 변경사항 확인
pnpm cdk deploy <StackName>   # 특정 스택 배포
```

### CDK Stacks (9개)

| Stack | Description |
|-------|-------------|
| EcrStack | ECR 리포지토리 (rca-agent, healthcare, cc-headless) |
| NetworkStack | VPC (Public + Private subnets, NAT Gateway) |
| EventBusStack | SNS Alarm Topic + SQS Queue (Fargate용) + DLQ |
| DatabaseStack | DynamoDB RCA 세션 테이블 |
| StorageStack | S3 Evidence/Report 버킷 + S3 Vectors (플레이북/보고서 임베딩) |
| RdsStack | PostgreSQL 17.4 (Healthcare 서비스용) |
| RcaAgentServiceStack | ECS Fargate — Strands RCA 에이전트 |
| CcHeadlessStack | ECS Fargate — CC headless RCA 에이전트 |
| HealthcareServiceStack | ECS Fargate — Healthcare 센서 서비스 + Cloud Map Private DNS |

모든 서비스는 Private subnet에 배포되며, 인바운드 트래픽이 차단됩니다. 자세한 스택 의존관계와 IAM 권한은 [`packages/infra/AGENTS.md`](../packages/infra/AGENTS.md)를 참조하세요.

## Agent — Fargate (Strands)

에이전트는 ECS Fargate 태스크로 배포됩니다. SQS 큐를 Long Polling으로 구독하며, 알람 메시지 수신 시 RCA 워크플로우를 자동 시작합니다.

## Agent — Fargate (CC Headless)

CC Headless 에이전트는 ECS Fargate 태스크로 배포됩니다. SQS 큐를 Long Polling으로 구독하며, CC CLI를 subprocess로 호출하여 RCA를 수행합니다.

```bash
cd packages/cc-headless
docker build -t cc-headless .
```

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
