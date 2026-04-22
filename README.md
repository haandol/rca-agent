# RCA Agent — AWS 기반 자동 RCA 분석 에이전트

AWS 환경에서 메트릭 알람 발생 시 자동 RCA(근본원인분석)를 수행하는 에이전트 시스템의 모노레포입니다. 두 가지 실행 엔진(Strands Agents SDK 파이프라인 / CC on Bedrock headless 프롬프트 주도)을 지원하며, MCP 서버를 통해 CloudWatch, CloudTrail, GitHub 데이터 소스를 자동 분석합니다.

## 패키지 개요

- **`packages/agent`** – Strands Agents SDK 기반 RCA 에이전트 (Fargate). 10단계 파이프라인(스코핑 → 가설 생성 → 우선순위 → 증거 수집 → 검증 → 분기 → 종료 판단 → 보고서 → 플레이북 → 알림)을 실행합니다. Planning 티어(Sonnet 4.6 + adaptive thinking)와 Execution 티어(Haiku 4.5)의 2-tier 모델 아키텍처를 사용합니다.
- **`packages/cc-headless`** – CC on Bedrock headless 기반 서버리스 RCA 에이전트 (Lambda). Claude Code CLI를 subprocess로 호출하여 단일 프롬프트로 전체 RCA 워크플로우를 수행합니다. MCP 도구를 자율적으로 호출합니다.
- **`packages/infra`** – AWS CDK 인프라. ECS Fargate, Lambda Container, SNS/SQS, S3, S3 Vectors, DynamoDB, VPC/PrivateLink, Bedrock 접근 등 전체 인프라를 코드로 관리합니다.
- **`packages/healthcare-sensor-app`** – 헬스케어 센서 데이터 수집/조회 서비스. RCA 에이전트 검증용 장애 주입(fault injection)을 지원합니다.

## 주요 기능

### Dual-Stack RCA 실행
- **Fargate Stack (Strands)**: CloudWatch Alarm → SNS → SQS → ECS Fargate, 10단계 파이프라인
- **Lambda Stack (CC Headless)**: CloudWatch Alarm → SNS → SQS → Lambda Container, 프롬프트 주도 RCA
- 동일 SNS 토픽을 독립 구독하여 A/B 비교 가능
- DynamoDB `engine` 필드로 실행 엔진 구분, `IDEMP#` 키로 멱등성 보장

### Fargate Stack
- LLM 기반 가설 생성 및 우선순위 결정 (Amazon Bedrock Claude)
- 2-tier 모델 아키텍처: Planning(Sonnet 4.6 + adaptive thinking) / Execution(Haiku 4.5)
- 가설-트리 기반 점진적 추론 및 가지치기
- 전체 기각 시 자동 가설 재생성 (최대 2회)
- 신뢰도/시간/깊이/루프 기반 종료 조건으로 운영 통제

### Lambda Stack
- Claude Code CLI headless 모드 + Bedrock 백엔드
- 단일 프롬프트에 5단계 RCA 워크플로우 정의, CC가 MCP 도구 자율 호출
- 프롬프트 내 10분 시간 예산 관리 (Lambda 15분 타임아웃)
- MCP 서버 구성: CloudWatch, CloudTrail, GitHub (`mcp-config.json`)

### 공통
- CloudWatch + CloudTrail + GitHub MCP 서버를 통한 데이터 자동 수집
- RCA 보고서 자동 생성 및 S3 저장
- 플레이북 검색 우선(search-first) 전략: 유사 플레이북 업데이트 또는 신규 생성
- S3 Vectors 기반 과거 장애 플레이북 유사도 검색
- SNS 알림 전송 (Presigned URL 보고서 링크 포함)
- Adaptive thinking 피처플래그 (`THINKING_ENABLED`, Fargate 전용)로 즉시 on/off

## 빠른 시작

```bash
pnpm install

# 전체 빌드
pnpm nx run-many -t build

# 전체 테스트
pnpm nx run-many -t test

# 전체 린트
pnpm nx run-many -t lint

# 에이전트 패키지 단독 테스트
cd packages/agent
uv sync --extra dev
uv run pytest tests/

# CC Headless 패키지 단독 테스트
cd packages/cc-headless
pnpm test

# 의존 관계 그래프 확인
pnpm nx graph
```

## 환경 설정

### Fargate Stack (packages/agent)

`python-dotenv`를 사용하여 `packages/agent/env/local.env`에서 설정을 로드합니다 (`override=False`, 기존 환경변수 우선).

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `BEDROCK_MODEL_ID` | `global.anthropic.claude-sonnet-4-6` | Planning 티어 모델 |
| `BEDROCK_HAIKU_MODEL_ID` | `global.anthropic.claude-haiku-4-5-20251001-v1:0` | Execution 티어 모델 |
| `THINKING_ENABLED` | `false` | Adaptive thinking 피처플래그 |
| `SQS_QUEUE_URL` | - | SQS 큐 URL (필수) |
| `S3_VECTOR_BUCKET_NAME` | - | S3 Vectors 버킷 |
| `S3_REPORT_BUCKET` | - | 보고서 저장 S3 버킷 |
| `SNS_NOTIFICATION_TOPIC_ARN` | - | RCA 완료 알림 SNS 토픽 |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | - | GitHub MCP 인증 (선택) |

전체 환경변수 목록은 [`packages/agent/env/local.env`](./packages/agent/env/local.env)를 참조하세요.

### Lambda Stack (packages/cc-headless)

Lambda 환경변수로 설정됩니다 (CDK 스택에서 자동 주입).

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `CLAUDE_CODE_USE_BEDROCK` | `1` | Bedrock 백엔드 활성화 |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `global.anthropic.claude-sonnet-4-6` | CC 기본 모델 |
| `DYNAMODB_TABLE_NAME` | - | 공유 RCA 세션 테이블 |
| `S3_EVIDENCE_BUCKET` | - | 공유 증거 버킷 |
| `S3_VECTOR_BUCKET_NAME` | - | 공유 S3 Vectors 버킷 |
| `S3_REPORT_BUCKET` | - | 공유 보고서 버킷 |
| `SNS_NOTIFICATION_TOPIC_ARN` | - | 알림 토픽 |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | - | GitHub MCP 인증 (선택) |

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
