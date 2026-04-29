# RCA Agent Project Guide

RCA Agent는 AWS 기반 자동 RCA(근본원인분석) 에이전트 시스템의 Nx 모노레포(pnpm workspace)입니다.

## Repository Structure

| Package | Description | Tech |
|---------|-------------|------|
| [`packages/agent`](./packages/agent/) | Strands Agents SDK 기반 RCA 에이전트 — 9단계 파이프라인 (단일 Sonnet + Planning/Execution 행동 분리) | Python, Strands Agents SDK, Amazon Bedrock |
| [`packages/infra`](./packages/infra/AGENTS.md) | AWS CDK 인프라 — ECS Fargate, SNS/SQS, S3, S3 Vectors, VPC, Cloud Map | TypeScript, CDK |
| [`packages/cc-headless`](./packages/cc-headless/AGENTS.md) | CC on Bedrock headless 기반 RCA 에이전트 — ECS Fargate에서 SQS Long Polling + CC CLI로 단일 프롬프트 RCA 수행 | Python, Claude Code CLI, ECS Fargate |
| [`packages/healthcare-sensor-app`](./packages/healthcare-sensor-app/AGENTS.md) | 헬스케어 센서 데이터 수집/조회 서비스 — 영구 지속형 장애 주입 + reset API, background traffic generator | Python, FastAPI, SQLAlchemy, PostgreSQL, OpenTelemetry |
| [`packages/dashboard`](./packages/dashboard/AGENTS.md) | RCA 대시보드 — DynamoDB 세션 상태, S3 보고서/플레이북/증거 조회, 파이프라인 트레이스 그래프 (로컬 전용) | TypeScript, Nuxt.js 4, TailwindCSS 4, DaisyUI 5, Vue Flow |

## Architecture at a Glance

동일한 CloudWatch 알람에 대해 두 가지 실행 엔진(Strands, CC Headless)이 독립적으로 RCA를 수행하며, SNS/DynamoDB/S3/S3 Vectors를 공유합니다.

> 시스템 다이어그램, 9단계 파이프라인, 모듈 간 데이터 흐름, Hexagonal 구조, Technology Stack 전체 내용은 **[docs/architecture.md](./docs/architecture.md)** 참조.

## Quick Start

Prerequisites와 환경 설정은 각 패키지의 AGENTS.md를 참조하세요.

```bash
pnpm install
pnpm nx run-many -t build
pnpm nx run-many -t test
```

## Agent Work Protocol

메인 에이전트는 오케스트레이터 역할을 하고, 패키지별 작업은 각 패키지의 AGENTS.md를 읽는 서브 에이전트에게 위임합니다.

- **Development Cycle**: Review/Create ADR → Implement → Build/lint → Test → Sync ADR → Commit
- **New feature**: 관련 ADR을 먼저 읽거나 새로 작성한 후 구현
- **Bug fix**: ADR 업데이트 불필요 (아키텍처 변경이 없는 경우)
- **Before commit**: 구현이 ADR과 달라졌으면 ADR과 `docs/adr/README.md` 인덱스 업데이트
- **Cross-package 순서**: Infra → Agent → Dashboard (의존성 하향)

> 원칙, Sub-Agent 정의표, Orchestrator 책임, ADR Workflow 전체는 **[docs/agent-protocol.md](./docs/agent-protocol.md)** 참조.

## Architecture Decision Records

`docs/adr/` — 새로운 기능이나 주요 변경 시 ADR 작성이 필수입니다. ADR은 **한국어**로 작성합니다.

- 전체 인덱스: **[docs/adr/README.md](./docs/adr/README.md)**
- 작성 규칙 및 워크플로우: [docs/agent-protocol.md#adr-workflow](./docs/agent-protocol.md#adr-workflow)

## Deployment & Testing

```bash
# Infra
cd packages/infra
pnpm nx deploy infra

# 테스트
pnpm nx run-many -t test
pnpm nx affected -t test
```

> CDK 스택 구성(9개), 패키지별 배포, RCA 정확도 테스트 지표는 **[docs/deployment.md](./docs/deployment.md)** 참조.

## Reference Documents

| Document | Description |
|----------|-------------|
| [Architecture](./docs/architecture.md) | Dual-Stack 다이어그램, 9단계 파이프라인, Data Flow, Hexagonal, Technology Stack |
| [Agent Protocol](./docs/agent-protocol.md) | Development Cycle, Sub-Agent Delegation, ADR Workflow |
| [Deployment](./docs/deployment.md) | CDK 스택, 패키지별 배포, 테스트 가이드 |
| [PRD](./docs/prd/aws-rca-agent-prd.md) | 제품 요구사항 정의서 — 기능 명세, 데모 시나리오, KPI |
| [아키텍처 & 데모 플로우](./docs/architecture-and-demo-flow.md) | 데이터 플로우, 상태 전이, 데모 시나리오 머메이드 다이어그램 |
| [ADR Index](./docs/adr/README.md) | 아키텍처 결정 기록 인덱스 |
| [운영 가이드](./docs/system-guide-for-ops.md) | 주니어 DevOps 운영팀원을 위한 시스템 안내서 |
| [Contributing Guide](./CONTRIBUTING.md) | 커밋 메시지, 브랜치 전략, PR 규칙 |
