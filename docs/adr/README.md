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
├── tools/        # MCP 도구 관련 결정 (CloudWatch, Logs, CloudTrail, GitHub 등)
└── infra/        # 인프라 관련 결정 (ECS Fargate, SNS/SQS, DynamoDB, S3, VPC 등)
```

각 카테고리 폴더에는 번호 매겨진 ADR과, 그 카테고리의 주요 결정 전환을 역순으로
기록하는 `decision-log.md`가 있다. ADR 본문은 현재 상태만 서술하고 진화 이력은
결정 로그와 Git이 보존한다.

## ADR 인덱스

ADR 목록은 `.mapping.json`이 유일한 인덱스다 — 카테고리별로 각 ADR의 경로, Status,
한 줄 Key Decision 요약과 카테고리 간 `dependsOn`을 담는다. README는 목록을
중복하지 않는다.

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
