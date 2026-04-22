# Architecture Decision Records (ADR)

이 디렉토리는 RCA Agent(AWS 기반 자동 RCA 분석 에이전트) 프로젝트의 주요 아키텍처 결정을 문서화합니다.

## ADR이란?

Architecture Decision Record (ADR)는 소프트웨어 개발 과정에서 내린 중요한 아키텍처 결정을 기록하는 문서입니다. 각 ADR은 다음을 포함합니다:

- **Context**: 결정이 필요했던 배경과 문제
- **Decision**: 내린 결정과 그 이유
- **Consequences**: 결정의 긍정적/부정적 영향

## 디렉토리 구조

```
adr/
├── agent/        # RCA 에이전트 코어 관련 결정 (가설-트리, 상태 머신, 프롬프트 등)
├── tools/        # MCP 도구 관련 결정 (CloudWatch, Logs, X-Ray, CloudTrail 등)
├── infra/        # 인프라 관련 결정 (ECS Fargate, SNS/SQS, DynamoDB, S3, VPC 등)
└── web/          # RCA 대시보드 웹 프론트엔드 관련 결정
```

## 카테고리별 ADR 목록

### Agent

- [ADR 0001: 초기 스코핑 전략 — 얕은 스코핑 + 유사 플레이북 검색](agent/0001-initial-scoping-strategy.md)
- [ADR 0002: 가설 생성 — 스코핑 기반 초기 가설 트리 구성](agent/0002-hypothesis-generation.md)
- [ADR 0003: 가설 우선순위 — 신뢰도 기반 검증 순서 결정](agent/0003-hypothesis-prioritization.md)
- [ADR 0004: 가설 검증/기각 — 증거 기반 가설 상태 전이](agent/0004-hypothesis-validation-pruning.md)
- [ADR 0005: 가설 분기 — 검증 중 발견된 새 가설 동적 추가](agent/0005-hypothesis-branching.md)
- [ADR 0006: 종료 조건 — RCA 분석 자동 종료 판단](agent/0006-termination-conditions.md)
- [ADR 0007: RCA 보고서 생성 — 구조화된 분석 결과 보고](agent/0007-rca-report-generation.md)
- [ADR 0008: 플레이북 생성 — RCA 결과 기반 재사용 가능 플레이북](agent/0008-playbook-generation.md)
- [ADR 0009: 알림 — RCA 진행/완료 알림 전달](agent/0009-notification.md)
- [ADR 0010: 모델 티어 아키텍처 — 계획/실행 모델 분리 + adaptive thinking](agent/0010-model-tier-architecture.md)
- [ADR 0011: CC on Bedrock headless 기반 프롬프트 주도 RCA 파이프라인](agent/0011-cc-headless-prompt-driven-rca.md)

### Tools

- [ADR 0001: 메트릭 수집 — CloudWatch MCP 서버 기반 증거 수집](tools/0001-metrics-collection.md)
- [ADR 0002: 로그 검색 — CloudWatch MCP 서버 기반 증거 수집](tools/0002-log-search.md)
- [ADR 0003: 트레이스 분석 — X-Ray 기반 분산 트레이스 조회](tools/0003-trace-analysis.md)
- [ADR 0004: 배포 이력 조회 — CloudTrail MCP 서버 기반 배포/변경 이벤트 조회](tools/0004-deploy-history.md)
- [ADR 0005: 코드 변경 분석 — GitHub MCP 서버 기반 배포 코드 diff의 LLM 결함 탐지](tools/0005-code-change-analysis.md)

### Infra

- [ADR 0001: 알람 수신 아키텍처 — SNS + SQS + ECS Fargate](infra/0001-alarm-ingestion-sns-sqs-fargate.md)
- [ADR 0002: 증거 저장 — S3 + S3 Vectors + DynamoDB 기반 증거 아카이브](infra/0002-evidence-storage.md)
- [ADR 0003: Lambda + CC on Bedrock headless 스택 — 서버리스 RCA 실행 인프라](infra/0003-lambda-cc-headless-stack.md)
- [ADR 0004: RDS PostgreSQL + Healthcare 서비스 배포 — RCA 검증용 데모 인프라](infra/0004-rds-healthcare-deployment.md)

### Web

(아직 등록된 ADR 없음)

## ADR 작성 가이드

새로운 ADR을 작성할 때는 `TEMPLATE.md` 템플릿을 사용하세요.

## 작성 규칙

- ADR에는 **구현 파일 경로를 포함하지 않는다.** ADR은 아키텍처 결정(Context, Decision, Consequences)을 기록하는 문서이며, 실제 수정할 파일 목록은 구현 시점에 코드베이스를 직접 확인하여 결정한다. 파일 경로는 리팩토링/이동에 의해 쉽게 무효화되므로 ADR의 유지보수 부담을 줄이기 위해 제외한다.
- 기존 ADR 중 구현 파일 경로가 포함된 것은 해당 ADR이 업데이트될 때 점진적으로 제거한다.

## 명명 규칙

- 파일명: `XXXX-kebab-case-title.md`
- 번호는 카테고리 내에서 순차적으로 증가
- 제목은 명확하고 간결하게

## 참고

- [ADR GitHub](https://adr.github.io/)
