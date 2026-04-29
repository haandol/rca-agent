# ADR 0011: CC on Bedrock headless 기반 프롬프트 주도 RCA 파이프라인

Date: 2026-04-22
Updated: 2026-04-24

## Status

Accepted (Updated — 산출물 파일 기반 트레이싱, JSON 중간 산출물, 서브에이전트 패턴)

## Context

현재 RCA 에이전트(agent/0001~0010)는 Strands Agents SDK를 사용하여 9단계 파이프라인(F1~F9: 스코핑·가설 생성·우선순위·증거 수집·검증·분기·보고서·플레이북·알림)을 Python 코드로 직접 구현한다. 각 단계마다 전용 Agent 인스턴스를 생성하고, Pydantic structured output으로 단계 간 데이터를 전달하며, 가설 트리 탐색 루프를 Python 코드로 오케스트레이션한다. 이 접근법은 세밀한 제어가 가능하지만 다음 한계가 있다:

1. **구현 복잡도**: 10개 에이전트 팩토리, 10개 프롬프트, 8개 Pydantic 모델, 루프/분기/종료 로직을 모두 직접 작성해야 한다
2. **모델 업그레이드 부담**: 모델 변경 시 SDK 호환성, structured output 포맷, thinking 파라미터를 모두 검증해야 한다
3. **도구 통합 유지보수**: MCP 서버 추가/변경 시 팩토리 함수 수정, 프롬프트 업데이트, 테스트 추가가 필요하다

**대안: Claude Code on Bedrock headless 모드**를 사용하면, Claude Code CLI가 프롬프트 해석 → MCP 도구 호출 → 결과 종합 → 구조화된 출력 반환을 자율적으로 수행한다. 에이전트 구현을 프롬프트 엔지니어링으로 대체하여 코드량을 대폭 줄일 수 있다. `agentic-app-engine-poc` 프로젝트에서 이 패턴이 검증되었다.

동일한 SNS 알람을 입력으로 받되, ECS Fargate에서 CC headless를 실행한다(초기 Lambda 설계에서 전환, infra/0003 참조).

추가로, CC headless의 **파이프라인 트레이싱**에 문제가 있었다. MCP 도구(`start_span`, `end_span`)로 CC CLI에게 직접 DDB 스팬을 기록하게 했으나, CC CLI가 "분석에 불필요한 도구"로 판단하여 호출을 건너뛰었다. 이를 해결하기 위해 **산출물 파일 기반 트레이싱**으로 전환하여, Python wrapper가 파일 생성을 감지하여 DDB에 스팬을 자동 기록한다.

## Decision

**Claude Code on Bedrock headless 모드**를 RCA 실행 엔진으로 사용하는 별도 ECS Fargate 스택을 추가한다. 기존 Strands SDK 기반 Fargate 스택과 **병렬로 공존**한다.

### 핵심 결정사항

1. **실행 방식**: ECS Fargate Task에서 Python wrapper가 Claude Code CLI를 subprocess로 호출한다. `CLAUDE_CODE_USE_BEDROCK=1` 환경변수로 Bedrock 백엔드를 활성화하고, `--output-format json` 플래그로 구조화된 결과를 받는다. 인프라 상세는 infra/0003을 참조한다.

2. **프롬프트 주도 파이프라인**: Python/SDK로 다단계를 오케스트레이션하는 대신, 단일 프롬프트에 RCA 전체 워크플로우를 지시한다. CC headless가 서브에이전트를 스폰하며 스코핑 → 가설 생성 → 검증 루프 → 보고서 → 자동 복구를 한 번의 호출로 수행한다.

3. **서브에이전트 패턴**: CC headless는 Agent tool을 사용하여 서브에이전트를 스폰한다:
   - **가설 생성 서브에이전트**: 스코핑 결과로부터 3-5개 근본원인 가설을 생성
   - **가설 검증 서브에이전트**: 검증 루프 1회를 수행 — 우선순위 결정, 증거 수집, 검증, 분기
   메인 에이전트는 서브에이전트 결과를 받아 `save_artifact`로 JSON 산출물을 저장한다.

4. **MCP 서버 연결**: CC headless의 MCP 설정(`mcp-config.json` + `--mcp-config`)으로 AWS Knowledge MCP, CloudWatch MCP, CloudTrail MCP, GitHub MCP, rca-progress MCP를 등록한다. rca-progress MCP는 `save_artifact` 도구만 제공하며, 산출물 파일을 `/tmp/rca-{id}/`에 저장한다.

5. **프롬프트 구성**:
   - **시스템 프롬프트**: RCA 워크플로우 정의 (스코핑 → 가설 생성 → 검증 루프 → 보고서 → 자동 복구 → 복구 검증), 종료 조건, JSON 산출물 스키마
   - **사용자 프롬프트**: 알람 페이로드 (AlarmName, StateReason, Trigger, Dimensions 등)를 템플릿에 주입
   - **워크스페이스 CLAUDE.md**: MCP 도구 사용 규칙, 트레이싱 데이터 흐름, 산출물 관리 규칙을 정의

6. **JSON 중간 산출물**: CC headless의 모든 중간 산출물은 정의된 JSON 스키마로 저장한다. Python wrapper가 이 파일들을 파싱하여 DDB에 트레이스를 기록한다.

   | 산출물 | 형식 | 내용 |
   |--------|------|------|
   | `scoping.json` | JSON | 스코핑 결과 (영향범위, 심각도, 메트릭 스냅샷) |
   | `hypotheses.json` | JSON | 가설 목록 (hypothesis_id, description, category 등) |
   | `validation-{N}.json` | JSON | N번째 검증 루프 결과 (confirmed, rejected, needs_investigation) |
   | `playbook.json` | JSON | 플레이북 (장애유형, 증상패턴, 검증절차, 복구방안) |
   | `report.md` | Markdown | 최종 RCA 보고서 |

7. **산출물 파일 기반 트레이싱**: CC CLI에게 MCP 도구로 직접 스팬을 기록하게 하는 방식은 CC CLI가 "분석에 불필요한 도구"로 판단하여 호출을 건너뛰는 문제가 있었다. 이를 해결하기 위해 Python wrapper의 `artifact_watcher`가 `/tmp/rca-{id}/` 디렉토리를 3초 간격으로 폴링하여 새 파일을 감지하고, JSON을 파싱하여 DDB에 스팬을 자동 기록한다. CC CLI는 분석에만 집중하고, 트레이싱은 Python wrapper가 전담한다. CC CLI 종료 후 `watcher_stop.set()` → 최종 `_scan_once()` 1회 추가 실행으로 마지막 산출물 누락을 방지한다. 상세는 infra/0005를 참조한다.

8. **출력 포맷**: 최종 출력은 `report.md`로 저장되는 한글 Markdown RCA 보고서이다. Python wrapper가 이 파일을 읽어서 S3에 업로드한다.

9. **세션 상태 관리**: 기존 Fargate 스택과 동일한 DynamoDB 테이블에 세션을 기록한다. `engine` 필드로 실행 엔진을 구분한다 (`strands` vs `cc-headless`). SK 접두사에 엔진명을 포함하여 엔진별 트레이스를 분리한다. `update_item`에 `attribute_exists(SK)` 조건을 추가하여 phantom HYPO 레코드 생성을 방지한다. 상태 전이는 `VALID_TRANSITIONS` 딕셔너리 기반 state machine으로 검증한다. CC Headless의 상태 전이는 `ALARM_RECEIVED → {ANALYZING, FAILED, CANCELLED}`, `ANALYZING → {COMPLETED, FAILED, CANCELLED}`로 단순하다. DDB ConditionExpression 가드는 동시성 보호를 위해 병행 유지한다.

10. **미검증 가설 자동 기각**: 검증 루프 종료 시 PENDING/NEEDS_INVESTIGATION 상태의 가설을 마지막 validation JSON의 `rejected`에 포함하도록 프롬프트에서 유도한다. Strands 엔진에서는 코드로 직접 REJECTED 상태를 기록한다. 이를 통해 세션 완료 시 모든 가설이 최종 상태(CONFIRMED/REJECTED)를 갖게 된다.

### 기존 Strands 스택과의 차이

| 항목 | Strands 스택 (기존) | CC Headless 스택 |
|------|--------------------|--------------------|
| 실행 엔진 | Strands Agents SDK | CC on Bedrock headless |
| 오케스트레이션 | Python 코드 (9단계 루프) | 프롬프트 주도 (CC 자율 실행 + 서브에이전트) |
| 컴퓨팅 | ECS Fargate (상시 실행) | ECS Fargate (상시 실행) |
| 트레이싱 | Python 코드에서 직접 DDB 쓰기 | 산출물 파일 감지 → artifact_watcher → DDB |
| MCP 연결 | SDK MCPClient 팩토리 | CC MCP 설정 파일 |
| 모델 제어 | 단계별 Planning/Execution 분리 | CC가 단일 모델로 자율 실행 |
| 중간 산출물 | Pydantic structured output | JSON 파일 (`save_artifact` MCP 도구) |

### 공유 인프라

두 스택은 다음 인프라를 공유한다:
- SNS Alarm Topic (동일 알람을 양쪽에서 수신)
- DynamoDB RCA 세션 테이블
- S3 보고서 버킷
- SNS 알림 Topic

## Consequences

### Positive

- 에이전트 구현 코드 대폭 감소 — 10개 에이전트 팩토리/프롬프트/모델이 단일 프롬프트로 통합
- CC headless가 모델 업그레이드, MCP 프로토콜 변경을 자체 흡수하여 유지보수 부담 감소
- 기존 Strands 스택과 병렬 실행하여 A/B 비교 가능
- 산출물 파일 기반 트레이싱으로 CC CLI가 분석에만 집중할 수 있고, 트레이스 누락 문제가 해결됨
- JSON 중간 산출물로 구조화된 데이터가 DDB에 저장되어 대시보드에서 가설 트리와 검증 과정을 시각화 가능

### Negative

- CC headless의 자율 실행으로 단계별 세밀한 제어(모델 티어 분리, 가설 트리 깊이 제한)가 어려움
- CC headless의 토큰 사용량과 도구 호출 횟수가 프롬프트 지시에 의존하여 비용 예측이 어려움
- CC CLI가 malformed JSON을 출력하면 해당 단계의 트레이스가 에러로 기록된다
- 플레이북 생성 실패 시 FAILED 스팬만 기록되고 파이프라인은 계속 진행 — 알림과 세션 완료에 영향 없음

### Risks

- CC headless가 프롬프트 지시를 무시하고 과도한 도구 호출을 수행할 수 있다. `--max-turns` 플래그로 최대 턴 수를 제한하여 완화한다.
- 프롬프트 변경이 RCA 품질에 직접 영향을 미치므로, 프롬프트 버전 관리와 품질 회귀 테스트가 필수이다.
- CC CLI가 `save_artifact`를 호출하지 않으면 트레이스가 생성되지 않는다. 시스템/사용자 프롬프트와 CLAUDE.md에서 반복적으로 산출물 저장을 강조하여 완화한다.

## Related

- [ADR agent/0010: 모델 티어 아키텍처](0010-model-tier-architecture.md) — Strands 스택의 모델 분리 전략 (CC Headless에서는 CC가 단일 모델로 대체)
- [ADR infra/0001: 알람 수신 아키텍처](../infra/0001-alarm-ingestion-sns-sqs-fargate.md) — 기존 Fargate 기반 수신 경로
- [ADR infra/0003: CC Headless 스택](../infra/0003-lambda-cc-headless-stack.md) — 이 ADR의 인프라 구현
- [ADR infra/0005: 실행 트레이스 DynamoDB](../infra/0005-execution-trace-dynamodb.md) — 산출물 파일 기반 트레이싱 상세
