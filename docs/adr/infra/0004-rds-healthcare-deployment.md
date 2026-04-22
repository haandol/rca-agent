# ADR 0004: RDS PostgreSQL + Healthcare 서비스 배포 — RCA 검증용 데모 인프라

Date: 2026-04-22

## Status

Accepted

## Context

RCA 에이전트의 정확도를 검증하려면 실제 AWS 환경에서 장애 시나리오를 재현해야 한다. Healthcare Sensor App은 이를 위한 데모 서비스로, 다음 요구사항이 있다:

1. **영속 스토리지**: 센서 데이터를 PostgreSQL에 저장하여 실제 운영 환경을 시뮬레이션
2. **CloudWatch baseline**: 정상 상태의 메트릭 패턴이 축적되어야 RCA 에이전트가 이상 탐지 가능
3. **장애 주입**: DB 커넥션 릭, 슬로우 쿼리, CPU/메모리 부하 등 fault injection 시나리오 지원
4. **네트워크 격리**: 외부 인바운드 차단, VPC 내부 통신만 허용

검토한 대안:
- **Aurora Serverless v2**: 자동 스케일링이 유연하지만, 데모 목적에 비해 비용이 과함
- **RDS t4g.micro**: 최소 비용으로 PostgreSQL 운영 가능, Free Tier 적용

## Decision

### RDS PostgreSQL 스택

독립 CDK 스택(`RdsStack`)으로 PostgreSQL 17.4를 배포한다.

- **인스턴스 타입**: t4g.micro (ARM64, 비용 최적화)
- **스토리지**: GP3 20GB (최대 50GB autoscaling)
- **네트워크**: Private subnet, VPC CIDR 내부에서만 5432 접근 허용
- **자격 증명**: Secrets Manager 자동 생성 (`{ns}/rds/postgres`)
- **보호 수준**: `deletionProtection: false`, `removalPolicy: DESTROY` (Dev 환경)

### Healthcare 서비스 배포

ECS Fargate로 Healthcare Sensor App을 배포한다.

- **DB 연결**: ECS 환경변수(`DB_HOST`, `DB_PORT`, `DB_NAME`) + Secrets Manager(`DB_USERNAME`, `DB_PASSWORD`)
- **Background Traffic Generator**: 앱 시작 시 asyncio task로 5초 간격 센서 데이터 자동 생성. 10명 가상 환자, 5가지 바이탈 타입, ~8% 비정상 값
- **Fault Injection**: 환경변수(`FAULT_DB_LEAK`, `FAULT_SLOW_QUERY_MS`, `FAULT_ERROR_RATE`)로 장애 활성화
- **Tracing**: OTel Collector 사이드카로 X-Ray 전송, `xray:PutTraceSegments` 권한 부여

### Cross-Stack 의존성 해결

RDS Security Group과 Healthcare ECS 서비스 간 cross-stack 참조로 CDK DependencyCycle이 발생할 수 있다. RDS SG에서 VPC CIDR 전체 대상으로 5432 인바운드를 허용하여 cross-stack SG 참조를 제거한다.

## Consequences

### Positive

- RCA 에이전트가 실제 AWS 환경에서 CloudWatch 메트릭/로그 기반으로 분석 가능
- Background traffic으로 정상 baseline이 자동 축적되어 fault injection 시 이상 패턴이 명확히 드러남
- t4g.micro + GP3로 최소 비용 운영

### Negative

- RDS 상시 실행 비용 발생 (t4g.micro ~$6/month)
- VPC CIDR 전체 인바운드 허용은 동일 VPC 내 다른 서비스의 DB 접근을 차단하지 못함 (Dev 환경에서만 허용)

### Risks

- Healthcare 서비스 재배포 시 circuit breaker가 DB 연결 실패로 롤백할 수 있다. RDS가 완전히 가동된 후 서비스를 배포해야 한다.
- Secrets Manager 비밀이 `fromGeneratedSecret`으로 자동 생성되므로, 스택 재생성 시 비밀이 변경된다.

## Related

- [ADR infra/0001: 알람 수신 아키텍처](0001-alarm-ingestion-sns-sqs-fargate.md) — Fargate 기반 서비스 배포 패턴
- [ADR infra/0002: 증거 저장](0002-evidence-storage.md) — S3/DynamoDB 공유 저장소
