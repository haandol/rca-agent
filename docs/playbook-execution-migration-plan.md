# 플레이북 실행 전환 — 남은 작업 인계 문서

> **이 문서의 목적**: 자동 복구를 사용자 승인 기반 플레이북 실행으로 전환하는 작업의
> 남은 단계를 새 세션이 이어받기 위한 인계서. 결정된 사항은 ADR이 보유하고, 이 문서는
> **무엇이 끝났고 무엇이 남았는지, 어디를 손대야 하는지**만 담는다.
>
> 목표 구조의 전체 그림은 [RCA에서 플레이북 실행까지](./rca-to-remediation-flow.md) 참조.

- 작업 브랜치: `feat/playbook-driven-remediation`
- 기준 커밋: `ff6c2ee`
- 전체 상태: **ADR 전체 + 모델 전환 + cc-headless 완료**, 실행 에이전트부터 남음
- 기준 시점 테스트: cc-headless 308 passed / agent 590 passed
  (agent 590은 아직 옛 자동 복구 테스트를 포함한 수치다 — C에서 줄어든다)

---

## 0. 새 세션 시작 절차

1. 루트 `AGENTS.md`와 대상 패키지의 `AGENTS.md`를 읽는다.
2. **ADR을 먼저 읽는다.** 이 전환의 모든 결정은 이미 ADR에 있으므로 새로 결정하지
   않는다. 핵심 4건:
   - `docs/adr/agent/0017-playbook-execution-agent.md` — 실행 주체, 승인 게이트,
     파괴적 액션 거부, 실행 증거, 해결 판정, 상태 전이
   - `docs/adr/agent/0018-playbook-retrospective.md` — 회고 진입 조건, 갱신 대상,
     축적 보존, 4단 열람 계약
   - `docs/adr/infra/0008-playbook-execution-stack.md` — 실행 스택, 요청 큐, 권한 격리
   - `docs/adr/agent/0008-playbook-generation.md` — 플레이북의 실행 절차 계약
3. 아래 4장에서 다음 단계를 선택하고, 그 단계의 완료 조건을 모두 만족시킨다.
4. 단계마다 커밋한다. 여러 단계를 한 커밋에 묶지 않는다.
5. 전체 완료 후 `/adr-sync`로 ADR↔코드 정합을 확인한다.

**ADR 변경이 필요해지면** 코드보다 ADR을 먼저 고치고 decision-log에 한 줄 남긴 뒤
같은 커밋에 포함한다.

---

## 1. 완료된 작업

### `9030937` 기본 모델 Sonnet 5 전환

`global.anthropic.claude-sonnet-5`로 전환하고 `temperature`를 제거했다. Bedrock
실측에서 확인한 제약 두 가지를 ADR 0010이 요구사항으로 보유한다.

| 파라미터 | Bedrock Sonnet 5 |
|----------|------------------|
| `temperature` | ❌ `deprecated for this model` |
| `effort` (thinking 하위/최상위 모두) | ❌ `Extra inputs are not permitted` |
| `thinking: {"type": "adaptive"}` | ✅ |
| forced `toolChoice` + adaptive | ✅ |

> ⚠️ 이 두 파라미터를 다시 넣으면 **모든 LLM 호출이 실패**한다. 회귀 테스트
> (`TestSamplingParameters`, `test_never_declares_an_effort_level`)가 막고 있다.

### `6423083` 흐름 문서 재작성

`docs/rca-to-remediation-flow.md`를 As-Is/To-Be 대조 문서로 다시 썼다.

### `9371300` ADR 전면 개정

| 성격 | ADR |
|------|-----|
| 신설 | `agent/0017` 실행 에이전트, `agent/0018` 회고, `infra/0008` 실행 인프라 |
| 대체 | `agent/0012` 자동 복구 → `Superseded by 0017` |
| 개정 | `agent/0007` 단일 리포트, `agent/0008` 플레이북=실행 근거(핵심 결정 반전), `agent/0009` 알림이 실행을 트리거하지 않음, `agent/0011` Remediation 단계 제거, `agent/0002` 원인 유형 소비처 변경, `agent/0016` 안전성 측정을 절차 내용으로, `infra/0002` 실행 증거 저장, `infra/0003` 분석 태스크 읽기 전용, `infra/0005` 실행 trace, `infra/0004`·`infra/0007` 전제 문장 정리 |

decision-log: agent 7줄, infra 4줄 추가. `.mapping.json` 27건 갱신.

### `ff6c2ee` cc-headless — 리포트 단일화 + 자동 복구 제거

**제거**: reset MCP 도구, `remediation_policy.py`, `post_reset_verification.py`,
remediation 역할·스킬·프롬프트 섹션, Healthcare 설정 전부. **분석 실행에 쓰기 도구가
하나도 없다.**

**추가**:
- `services/destructive_actions.py` — 거부 어휘의 **단일 소스**. 실행 에이전트(A)가
  이 모듈을 그대로 쓴다.
- `services/fault_taxonomy.py` — 원인 유형 분류 (실행 허용목록이 **아님**)
- 플레이북 `execution_steps` 계약: `step_id`, `intent`, `action`, `success_criteria`
- `verification_status: DRAFT`

**게이트 강화**: 리포트 서술의 단계 목록과 구조화 절차가 **같은 순서로 일치**해야
완료된다. 미확정 원인에 실행 절차를 선언하면 거부한다.

검증: 308 tests 통과.

> 작성 중 실제 결함 2건을 잡았다. (1) 순서 검증이 `in` 연산만 써서 순서를 보지 않았다.
> (2) 분석 설정에 reset 타임아웃이 남아 있었다.

---

## 2. 지금 시스템의 상태 — 복구 기능 공백

**현재 어떤 복구도 실행되지 않는다.** cc-headless의 in-process 복구를 제거했고
실행 에이전트는 아직 없다. Strands 복구 워커는 원래 `desiredCount=0`으로 꺼져 있었다.

이 공백은 A가 끝나면 닫힌다. 그 사이 분석·리포트·플레이북 생성은 정상 동작한다.

---

## 3. 아직 손대지 않은 코드 목록

새 세션이 grep으로 다시 찾지 않도록 미리 조사한 결과다.

### Strands (`packages/agent`)

옛 자동 복구 경로가 **그대로 남아 있다.**

| 파일 | 처리 |
|------|------|
| `src/rca_agent/remediation_main.py` | 제거 (복구 워커 진입점) |
| `src/rca_agent/services/remediation_pipeline.py` | 제거 (outbox·publication lease 포함) |
| `src/rca_agent/services/remediation.py` | 제거 (reset 호출) |
| `src/rca_agent/services/verification.py` | 제거 (M-of-N 정상화 판정) |
| `src/rca_agent/prompts/verification.py` | 제거 |
| `tests/test_remediation*.py` (3개), `tests/test_verification.py`, `tests/test_h07_remediation_outbox_integration.py` | 제거 또는 실행 트리거 테스트로 교체 |

리포트·플레이북 생성은 유지하되 **새 계약에 맞춰야** 한다.

| 파일 | 처리 |
|------|------|
| `services/report.py`, `prompts/report.py` | 리포트에 플레이북 포함, 실행 결과 제외 |
| `services/playbook_gen.py`, `prompts/playbook.py` | `execution_steps` + `verification_status` 추가 |
| `services/pipeline.py` | 완료 후 실행 트리거 (자동 복구 아님) |
| `services/notification.py` | 기계 소비자용 필드 정리 |
| `ports/dto/models.py` | `RemediationContext`·`RemediationResult`·`VerificationResult` 제거, `Playbook`에 실행 절차 추가 |
| `adapters/secondary/session/dynamodb_session_store.py` | remediation claim·publication lease 관련 메서드 정리 |

> **주의**: cc-headless와 Strands의 플레이북 스키마가 같아야 한다. 같은 S3 Vectors
> 인덱스를 공유하고 같은 실행 에이전트가 소비하기 때문이다. cc-headless의
> `services/artifact_validation.py`가 사실상 스키마 정의이므로 이를 기준으로 맞춘다.

### 인프라 (`packages/infra`)

| 대상 | 처리 |
|------|------|
| `lib/stacks/remediation-agent-stack.ts` | **삭제** (신규 스택으로 교체 — ADR infra/0008에서 신설 결정) |
| 신규 실행 스택 | 요청 큐 + 상시 워커 + 쓰기 권한 태스크 역할 |
| `bin/infra.ts` | 스택 교체, SNS 자동 구독 제거 |
| `config/dev.toml` | `[remediation]` 섹션 → 실행 스택 설정 |
| `lib/stacks/cc-headless-stack.ts` | Healthcare 환경변수·네트워크 인그레스 제거 (분석은 읽기 전용) |
| `test/remediation-agent-stack.test.ts` | 새 스택 테스트로 교체 |

### 대시보드 (`packages/dashboard`)

| 대상 | 처리 |
|------|------|
| `server/api/` 신규 | 실행 트리거(큐 발행), 실행 상세 조회, 회고 조회 |
| `server/utils/remediation.ts` | 실행 상태 정규화로 재작성 |
| `app/pages/report/[id].vue` | 플레이북을 리포트 안에서 렌더 + 실행 버튼 |
| `app/pages/playbook/[id].vue` | 리포트에 병합하거나 실행 이력 화면으로 전환 |
| `app/pages/index.vue` | 실행 상태 컬럼 (`승인 대기`/`실행 중`/`해결`/`미해결`/`실패`) |
| 신규 회고 화면 | 이슈·실행 전 플레이북·실행 증거·갱신 diff 4단 비교 |

### 문서

`docs/architecture.md`, `docs/architecture-and-demo-flow.md`,
`docs/system-guide-for-ops.md`, 루트 `AGENTS.md` — 흐름·상태·스택 서술 갱신.

`docs/rca-remediation-high-findings.md`의 미해결 항목 중 이 전환과 겹치는 것:
**H-16**(fault별 알람·검증 신호 정합, `IN_PROGRESS`), **H-17**·**H-18**(대시보드
삭제·취소의 fencing), **H-19**(대시보드 XSS — 새 화면 추가 시 함께 처리), **H-20**.

---

## 4. 남은 단계

### A. 플레이북 실행 에이전트 — 다음 작업

**성격**: 기존 코드 수정이 아니라 **새 실행 주체를 만드는 일**. 이 전환의 핵심이며
가장 큰 단계다.

근거 ADR: `agent/0017`, `infra/0008`

구현해야 할 것:

1. **실행 요청 소비** — SQS 폴링, 안정 식별자, claim으로 단일 실행 보장. 기존
   알람 수신 경로(`infra/0006`)의 재전달·멱등성 패턴을 재사용한다.
2. **플레이북 절차 → 명령 변환** — 리포트를 읽어 `execution_steps`를 수행. CC
   하네스 형태로 실행 에이전트가 직접 수행한다(별도 오케스트레이터를 두지 않음).
3. **파괴성 게이트** — `destructive_actions.py`(이미 있음)를 실행 도구에서 호출.
   판정 불가 명령은 **거부**한다. 차단은 증거에 기록하고 해당 절차를 수동 조치로
   남기되 실행 전체를 중단하지 않는다.
4. **실행 증거 누적** — 절차 식별자, 명령·인자, 종료 상태·오류, 실패 분류, 재시도·교정
   내역, 관측 결과. 오브젝트 저장소 주 보관 + 상태 저장소 요약(`infra/0002`).
   자격 증명으로 보이는 인자는 가린다.
5. **해결 판정** — 절차의 `success_criteria`를 관측해 판정. 관측으로 확정할 수 없으면
   완료로 전이하지 않는다.
6. **상태 전이** — `승인 대기 → 실행 중 → 검증 중 → 해결/미해결`, `실행 중 → 실패/취소`.
   분석 세션과 별도 생명주기이며 실행 실패가 리포트를 훼손하지 않는다.

완료 조건:
- 승인 없이 실행이 시작되지 않음을 테스트가 검증한다.
- 파괴적 명령과 판정 불가 명령이 차단되고 증거에 남는다.
- 실패한 실행의 증거가 보존된다.
- 같은 요청의 재전달이 중복 실행되지 않는다.
- 관측 실패가 해결로 추정되지 않는다.

### B. 회고 에이전트

근거 ADR: `agent/0018`

- 해결 확정 직후 자동 트리거 (미해결·실패는 진입하지 않음)
- 실행 증거에서 **절차 결함으로 환원되는 실패**만 교정. 일시적 오류는 교정하지 않는다
  (재시도로 성공했다면 절차는 옳았다).
- 삭제 금지를 **코드로 보장**. 모델이 필드를 누락하면 기존 값 유지.
- 갱신 전 플레이북 사본 보존 (diff 기준이 사라지지 않도록)
- 실행 단위 claim으로 중복 회고 차단
- 회고 실패는 실행 결과를 되돌리지 않는다

완료 조건: 미해결 실행이 회고로 진입하지 않고, 갱신이 기존 절차를 삭제하지 않으며,
4단 열람에 필요한 4종이 모두 조회 가능하다.

### C. Strands 정합

3장의 Strands 목록대로 처리. 자동 복구 경로를 제거하고 리포트·플레이북을 새 계약에
맞춘 뒤 실행 트리거를 붙인다. **dual-stack은 유지한다** — 엔진 비교 분석이 목적이며
두 엔진 모두 같은 실행 에이전트를 트리거한다.

### D. 인프라

3장의 인프라 목록대로 처리. `RemediationAgentStack`을 삭제하고 실행 스택을 신설한다.
배포 시 기존 리소스가 제거되며, 자동 복구는 기본 비활성이었으므로 진행 중 작업은 없다.

### E. 대시보드

3장의 대시보드 목록대로 처리. 실행 트리거가 **승인 게이트의 유일한 진입점**이므로
이 단계까지 끝나야 전환이 사용 가능해진다.

### F. 문서·정합

3장의 문서 목록 갱신 후 `/adr-sync`.

---

## 5. 순서에 대한 판단

의존성 하향(인프라 → 엔진 → 대시보드)이 원칙이지만, 이 전환에서는 **A를 먼저** 두었다.
실행 에이전트가 없으면 인프라 스택이 배포할 대상이 없고 대시보드가 트리거할 대상도
없다. A·B로 실행 주체를 완성한 뒤 C~E로 주변을 맞추는 순서가 각 단계의 검증을
독립적으로 만든다.

C·D·E는 서로 독립적이므로 순서를 바꿔도 된다. 다만 E(대시보드)가 마지막이어야 사람이
실제로 승인해 보는 E2E 확인이 가능하다.

---

## 6. 공통 검증

각 단계의 대상 테스트와 함께 실행한다.

```bash
pnpm verify
pnpm --filter infra build
pnpm --filter dashboard build
```

안전 경계를 건드린 단계는 다음 부정 테스트를 반드시 포함한다.

- 사용자 승인 없이 실행이 시작되지 않는다.
- 파괴적 액션과 판정 불가 명령이 차단된다.
- 미확정 원인의 플레이북에 실행 절차가 없다.
- 관측으로 확정할 수 없는 결과가 해결로 기록되지 않는다.
- 미해결·실패한 실행이 회고로 플레이북을 갱신하지 않는다.
- 실행 실패가 분석 리포트를 변경하지 않는다.

인프라 계약을 바꾼 단계는 CDK synth를 추가한다.

---

## 7. 확정된 설계 결정 (다시 논의하지 않음)

사용자가 이미 선택한 사항이다. ADR에 반영되어 있으며 재논의 대상이 아니다.

| 항목 | 결정 |
|------|------|
| 실행 권한 범위 | 대상 리소스 **제한 없음**, 파괴적 액션만 차단 |
| 파괴성 판정 방식 | **API 이름 기반 거부 목록** (허용 목록 아님) |
| 실행 스택 | **신규 스택 신설 + 기존 `RemediationAgentStack` 삭제** |
| 실행 트리거 | **SQS 큐에 실행 요청 발행** (ECS RunTask 아님) |
| 회고 시점 | **실행 성공 직후 자동** |
| 자동 복구 | **완전 제거** — 항상 사용자 승인 |
| dual-stack | **유지** (엔진 비교 분석 목적) |
| 기본 모델 | Sonnet 5, `temperature`·`effort` 미전달 |

---

## 8. 관련 문서

- [RCA에서 플레이북 실행까지](./rca-to-remediation-flow.md) — As-Is/To-Be 전체 그림
- [아키텍처](./architecture.md) — dual-stack, 파이프라인, 저장소 (갱신 필요)
- [운영 가이드](./system-guide-for-ops.md) — 인프라·데모 운영 (갱신 필요)
- [High 발견사항 추적](./rca-remediation-high-findings.md) — 미해결 H 항목
- ADR 인덱스: `docs/adr/.mapping.json`
