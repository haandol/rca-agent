# ADR 0011: CC on Bedrock headless 기반 프롬프트 주도 RCA 파이프라인

Date: 2026-04-22

## Status

Proposed

## Context

현재 RCA 에이전트(agent/0001~0010)는 Strands Agents SDK를 사용하여 10단계 파이프라인(F1~F10)을 Python 코드로 직접 구현한다. 각 단계마다 전용 Agent 인스턴스를 생성하고, Pydantic structured output으로 단계 간 데이터를 전달하며, 가설 트리 탐색 루프를 Python 코드로 오케스트레이션한다. 이 접근법은 세밀한 제어가 가능하지만 다음 한계가 있다:

1. **구현 복잡도**: 10개 에이전트 팩토리, 10개 프롬프트, 8개 Pydantic 모델, 루프/분기/종료 로직을 모두 직접 작성해야 한다
2. **모델 업그레이드 부담**: 모델 변경 시 SDK 호환성, structured output 포맷, thinking 파라미터를 모두 검증해야 한다
3. **도구 통합 유지보수**: MCP 서버 추가/변경 시 팩토리 함수 수정, 프롬프트 업데이트, 테스트 추가가 필요하다

**대안: Claude Code on Bedrock headless 모드**를 사용하면, Claude Code CLI가 프롬프트 해석 → MCP 도구 호출 → 결과 종합 → 구조화된 출력 반환을 자율적으로 수행한다. 에이전트 구현을 프롬프트 엔지니어링으로 대체하여 코드량을 대폭 줄일 수 있다. `agentic-app-engine-poc` 프로젝트에서 이 패턴이 검증되었다.

동일한 SNS 알람을 입력으로 받되, ECS Fargate 대신 **Lambda에서 CC headless를 실행**하여 서버리스로 운영한다. Lambda 15분 타임아웃 제약은 CC headless의 자율 탐색 범위를 시간 내로 제한하는 프롬프트 지시로 해결한다.

## Decision

**Claude Code on Bedrock headless 모드**를 RCA 실행 엔진으로 사용하는 별도 Lambda 기반 스택을 추가한다. 기존 Strands SDK 기반 Fargate 스택과 **병렬로 공존**한다.

### 핵심 결정사항

1. **실행 방식**: Lambda 함수 내에서 Claude Code CLI를 subprocess로 호출한다. `CLAUDE_CODE_USE_BEDROCK=1` 환경변수로 Bedrock 백엔드를 활성화하고, `--output-format json` 플래그로 구조화된 결과를 받는다.

2. **프롬프트 주도 파이프라인**: Python/SDK로 10단계를 오케스트레이션하는 대신, 단일 프롬프트에 RCA 전체 워크플로우를 지시한다. CC headless가 MCP 도구를 자율 호출하며 스코핑 → 가설 → 증거 수집 → 검증 → 보고서를 한 번의 호출로 수행한다.

3. **MCP 서버 연결**: CC headless의 MCP 설정(`.mcp.json` 또는 `--mcp-config`)으로 CloudWatch MCP, CloudTrail MCP, GitHub MCP를 등록한다. CC가 프롬프트 지시에 따라 도구를 자율 선택하여 호출한다.

4. **프롬프트 구성**:
   - **시스템 프롬프트**: RCA 워크플로우 정의 (스코핑 → 가설 → 증거 수집 → 검증 → 분기/종료 → 보고서), 종료 조건, 출력 포맷 (JSON schema)
   - **사용자 프롬프트**: 알람 페이로드 (AlarmName, StateReason, Trigger, Dimensions 등)를 템플릿에 주입
   - **워크스페이스 CLAUDE.md**: MCP 도구 사용 규칙, 증거 수집 패턴, 보고서 구조를 정의

5. **출력 포맷**: CC headless의 JSON 출력에서 RCA 결과를 파싱한다. 기존 Fargate 스택과 동일한 보고서 구조(incident_summary, root_cause, mitigation, remediation, timeline)를 프롬프트에서 지정한다.

6. **세션 상태 관리**: 기존 Fargate 스택과 동일한 DynamoDB 테이블에 세션을 기록한다. `engine` 필드로 실행 엔진을 구분한다 (`strands` vs `cc-headless`).

7. **타임아웃 전략**: Lambda 최대 타임아웃(15분)에 맞춰 프롬프트에 "10분 이내 분석 완료" 지시를 포함한다. 나머지 5분은 결과 파싱, S3 저장, SNS 알림에 사용한다.

### 기존 Fargate 스택과의 차이

| 항목 | Fargate 스택 (기존) | Lambda 스택 (신규) |
|------|--------------------|--------------------|
| 실행 엔진 | Strands Agents SDK | CC on Bedrock headless |
| 오케스트레이션 | Python 코드 (10단계 루프) | 단일 프롬프트 (CC 자율 실행) |
| 컴퓨팅 | ECS Fargate (상시 실행) | Lambda (이벤트 기반) |
| 타임아웃 | 제한 없음 (20분 버짓) | 15분 (Lambda 제한) |
| MCP 연결 | SDK MCPClient 팩토리 | CC MCP 설정 파일 |
| 모델 제어 | 단계별 Planning/Execution 분리 | CC가 단일 모델로 자율 실행 |
| 비용 모델 | 상시 실행 (고정비) | 호출당 과금 (변동비) |

### 공유 인프라

두 스택은 다음 인프라를 공유한다:
- SNS Alarm Topic (동일 알람을 양쪽에서 수신 가능)
- DynamoDB RCA 세션 테이블
- S3 증거/보고서 버킷
- S3 Vectors 플레이북 인덱스
- SNS 알림 Topic

## Consequences

### Positive

- 에이전트 구현 코드 대폭 감소 — 10개 에이전트 팩토리/프롬프트/모델이 단일 프롬프트로 통합
- 서버리스 운영으로 알람이 없는 시간에 컴퓨팅 비용 제로
- CC headless가 모델 업그레이드, MCP 프로토콜 변경을 자체 흡수하여 유지보수 부담 감소
- 기존 Fargate 스택과 병렬 실행하여 A/B 비교 가능

### Negative

- CC headless의 자율 실행으로 단계별 세밀한 제어(모델 티어 분리, 가설 트리 깊이 제한)가 어려움
- Lambda 15분 제한으로 복잡한 가설 트리 탐색이 중단될 수 있음
- CC CLI subprocess 호출 오버헤드로 Lambda 콜드스타트가 길어질 수 있음
- CC headless의 토큰 사용량과 도구 호출 횟수가 프롬프트 지시에 의존하여 비용 예측이 어려움

### Risks

- Lambda 컨테이너 이미지에 CC CLI 설치가 필요하여 이미지 크기가 증가한다. Lambda 컨테이너 이미지 10GB 제한 내에서 관리한다.
- CC headless가 프롬프트 지시를 무시하고 과도한 도구 호출을 수행할 수 있다. `--max-turns` 플래그로 최대 턴 수를 제한하여 완화한다.
- 프롬프트 변경이 RCA 품질에 직접 영향을 미치므로, 프롬프트 버전 관리와 품질 회귀 테스트가 필수이다.

## Related

- [ADR agent/0010: 모델 티어 아키텍처](0010-model-tier-architecture.md) — Fargate 스택의 모델 분리 전략 (Lambda 스택에서는 CC가 단일 모델로 대체)
- [ADR infra/0001: 알람 수신 아키텍처](../infra/0001-alarm-ingestion-sns-sqs-fargate.md) — 기존 Fargate 기반 수신 경로
- [ADR infra/0003: Lambda + CC headless 스택](../infra/0003-lambda-cc-headless-stack.md) — 이 ADR의 인프라 구현
