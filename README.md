# ALPS — AWS 기반 자동 RCA 분석 에이전트

AWS 환경에서 메트릭 알람 발생 시 가설-트리(Hypothesis Tree) 방식으로 자동 RCA(근본원인분석)를 수행하는 에이전트 시스템의 모노레포입니다. Strands Agents SDK와 MCP 서버를 활용하여 CloudWatch, X-Ray, CloudTrail 등 AWS 데이터 소스를 자동 분석하고, 근본 원인 도출부터 보고서 생성까지 전 과정을 자동화합니다.

## 패키지 개요

- **`packages/agent`** – Strands Agents SDK 기반 RCA 에이전트. 가설 생성기(Orchestrator)와 전문 툴-콜러(Tool-Caller) 에이전트로 구성. 가설-트리 탐색, 증거 기반 검증, 보고서/플레이북 생성을 수행합니다.
- **`packages/tools`** – MCP 도구 및 @tool 구현체. CloudWatch 메트릭 수집, 로그 검색, X-Ray 트레이스 분석, CloudTrail 배포 이력 조회, 코드 변경 분석, S3 Vectors 유사도 검색 도구를 제공합니다.
- **`packages/infra`** – AWS CDK 인프라. ECS Fargate, SNS/SQS, DynamoDB, S3, S3 Vectors, VPC/PrivateLink, Bedrock 접근 등 전체 인프라를 코드로 관리합니다.
- **`packages/web`** – RCA 대시보드 웹 프론트엔드. RCA 목록 조회, 가설 트리 시각화, 증거 상세 패널, 보고서 열람 기능을 제공합니다.

## 주요 기능

- CloudWatch Alarm → SNS → SQS → ECS Fargate 에이전트 자동 실행
- LLM 기반 가설 생성 및 우선순위 결정 (Amazon Bedrock Claude)
- CloudWatch Metrics/Logs, X-Ray, CloudTrail, GitHub/CodeCommit 자동 증거 수집
- 가설-트리 기반 점진적 추론 및 가지치기
- 신뢰도/비용/깊이 기반 중단 조건으로 운영 통제
- RCA 보고서 및 플레이북 자동 생성
- S3 Vectors 기반 과거 장애 플레이북/증거 유사도 검색
- SNS 알림 전송 (보고서 링크 포함)

## 빠른 시작

```bash
pnpm install

# 전체 빌드
pnpm nx run-many -t build

# 전체 테스트
pnpm nx run-many -t test

# 전체 린트
pnpm nx run-many -t lint

# 특정 패키지 빌드
pnpm nx build agent
pnpm nx build tools

# 의존 관계 그래프 확인
pnpm nx graph
```

개별 패키지 명령은 `pnpm nx run <project>:<target>` 형식으로 실행할 수 있습니다.

## 문서

- **PRD**: [docs/prd/aws-rca-agent-prd.md](./docs/prd/aws-rca-agent-prd.md)
- **ADR 모음**: [docs/adr/](./docs/adr/) — 아키텍처 결정 기록
- **패키지별 AGENTS**: 각 패키지의 AGENTS.md에서 세부 지침 확인

## 기여하기

커밋 메시지, 브랜치 전략, PR 규칙 등 기여 규칙은 [CONTRIBUTING.md](./CONTRIBUTING.md)를 따릅니다.

## 참고

- 패키지/기능을 수정할 때는 관련 ADR을 먼저 검토하고, 변경 사항이 설계에 영향을 주면 새로운 ADR을 작성하거나 기존 문서를 갱신합니다.
- 보안·인증·인프라 설정을 변경할 때는 최소 권한 원칙과 환경 변수 관리 원칙을 따르세요.
- 에이전트 프롬프트를 수정할 때는 반드시 시나리오 테스트셋으로 정확도를 검증합니다.
