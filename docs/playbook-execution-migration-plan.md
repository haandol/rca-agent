# 플레이북 실행 전환 — 완료 상태와 남은 작업

> **이 문서의 목적**: 자동 복구를 사용자 승인 기반 플레이북 실행으로 전환하는 작업의
> **현재 도달 상태**와 **남은 작업**을 기록한다. 결정된 사항은 ADR이 보유하고, 이
> 문서는 무엇이 끝났고 무엇이 남았는지만 담는다.
>
> 목표 구조의 전체 그림은 [RCA에서 플레이북 실행까지](./rca-to-remediation-flow.md) 참조.

- 기준: `8c40680` 이후 11개 커밋 (main, HEAD `b9bcc62`)
- 전체 상태: **A~F 전 단계 코드 완료**이고 R-1·R-2·R-4가 닫혔다. 라이브 실측을 4차까지
  진행해 **결함 5건을 찾아 전부 고쳤고**, 4차에서 닫힌 루프가 끝까지 돌았다. 남은 것은
  **R-3(CSP) 하나**다.
- 최초 인계 시점(`ff6c2ee`)의 문서는 `be7e227`에서 삭제됐다. 이 문서는 그 이후
  후속 커밋과 R-1·adr-sync·라이브 실측 4차까지 반영해 코드 기준으로 다시 쓴 것이다.

---

## 1. 검증 결과

R-1·R-4, `/adr-sync`, 대시보드 재작성, 실행 응답 로깅까지 반영한 값이다. 괄호는
`8c40680` 기준값이며, 늘어난 만큼이 이번 작업에서 추가한 테스트다.

| 대상 | 결과 |
|------|------|
| `pnpm verify` (format + lint + test + typecheck) | 통과 |
| agent unit | 506 passed (502) |
| cc-headless unit | 499 passed (486) |
| healthcare-sensor-app unit | 42 passed, 1 skipped, 1 xfailed |
| infra unit (CDK assertion) | 31 passed (5 suites, 기준 30) |
| 실행 하네스 계약 테스트 | 23 passed (기준 20) |
| 오프라인 평가 | 8 engine/scenario, digest `sha256:35683f45…` |
| `pnpm --filter infra build` | 통과 |
| `pnpm --filter dashboard build` | 통과 |
| `scripts/adr-invariants.sh` | 통과 (역참조 양방향 0건) |
| ADR↔코드 deep 정합 | 27건 검증, 요구사항 값 전량 일치, Status 전량 정확 |

> 플러그인 하네스(`adr-structure-lint.mjs`)는 error 664건을 보고하지만 **전부 `.nx/`
> 빌드 캐시와 `docs/test-reports/`의 시점 스냅샷**이다. 레포의 `adr-invariants.sh`가
> 이 경로들을 제외해 양방향 0건으로 판정하며, 판정 근거는 이 스크립트다.

---

## 2. 단계별 도달 상태

인계 문서의 A~F 단계를 현재 코드에 대조한 결과다.

### A. 플레이북 실행 에이전트 — 완료

`packages/cc-headless`에 실행 주체가 들어왔다. 분석 워커와 **같은 이미지의 다른
진입점**(`cc_headless.execution_main`)이다.

| 구성 요소 | 위치 |
|-----------|------|
| 진입점 | `src/cc_headless/execution_main.py` |
| 파이프라인 | `services/execution_pipeline.py` |
| 요청 소비·claim | `services/execution_request.py`, `adapters/secondary/execution/dynamodb_execution_store.py` |
| 파괴성 게이트 | `services/command_gate.py` + `services/destructive_actions.py` |
| 실행 증거 | `services/execution_evidence.py` |
| 해결 판정 | `services/execution_outcome.py` |
| 상태 전이 | `services/execution_state.py` |
| 실행 도구 | `execution_mcp_server.py` |
| 에이전트 정의 | `.claude-execution/agents/execution-operator.md` |

전용 테스트 10종(`test_command_gate.py`, `test_execution_*.py`,
`test_gate_prompt_alignment.py`)이 붙어 있다.

### B. 회고 에이전트 — 완료

`execution_pipeline.py`의 `_retrospect`가 `enters_retrospective(verdict.state)`
뒤에만 진입한다. 삭제 금지는 `services/playbook_merge.py`가 **코드로** 보장한다 —
갱신안에 없는 필드와 절차는 기존 값이 남고, 절차 순서는 유지된 뒤 새 절차만 뒤에
붙는다. 갱신 전 사본은 실행 **시작 시점**에 `save_playbook_snapshot`으로 보존한다.

### C. Strands 정합 — 완료

`remediation_main.py`, `services/remediation*.py`, `services/verification.py`,
`prompts/verification.py`와 관련 테스트가 모두 사라졌다. `services/` 아래 남은 것은
분석·리포트·플레이북 경로뿐이다. `Playbook` DTO에 `execution_steps`와
`verification_status`가 들어와 cc-headless의 `artifact_validation.py` 스키마와
맞았고, 하네스 계약 테스트가 두 엔진의 실행 절차 필드 일치를 강제한다. dual-stack은
유지됐다.

### D. 인프라 — 완료

`RemediationAgentStack`이 사라지고 `lib/stacks/playbook-execution-stack.ts`가
들어왔다. `bin/infra.ts`가 이를 배선하고 `config/dev.toml`에 `[execution]` 섹션이
있다. `deploy-service.sh`가 실행 워커를 인식한다(`deploy:service -- cc-headless
execution`). `test/playbook-execution-stack.test.ts`가 스택 계약을 검증한다.

### E. 대시보드 — 완료 (플레이북 화면 재작성 2026-07-31)

승인 게이트는 `app/pages/report/[id].vue`이며, 실행 진입점은
`server/api/executions.post.ts` 하나다. 이 핸들러는 워커가 거부할 요청을 미리
막는다 — 엔진 allowlist, `COMPLETED` 세션, 절차 0건 거부, 진행 중 실행 중복 거부,
`approvalId` 기반 멱등성. 회고 4단 열람은
`server/api/retrospectives/[rcaId]/[executionId].get.ts`가 이슈·실행 전 플레이북·
실행 증거·갱신 diff를 한 응답으로 돌려주고 전용 페이지가 렌더한다.
`server/utils/execution.ts`가 실행 상태를 정규화하고 `index.vue`가 실행 배지·차단
건수·시도 횟수를 보여준다.

플레이북 화면은 현재 구성과 어긋나 있어 재작성했다. 장애 유형·증상 패턴 같은 지식
필드만 렌더하고 **사람이 절차를 신뢰할지 판단하는 세 가지를 전부 빠뜨리고 있었다** —
실행 에이전트가 수행할 실행 절차, 초안인지 검증됨인지, 회고가 이미 교정했는지. 셋을
추가하고, 개정본이 있으면 회고 4단 비교로 링크한다. 실측에서 절차 4건과 초안 배지가
정상 렌더됨을 브라우저로 확인했다. Nuxt devtools 플로팅 버튼도 껐다 — 긴 목록의 우측
하단을 덮는다.

### F. 문서·정합 — 완료

`docs/rca-to-remediation-flow.md`가 As-Is/To-Be 대조에서 **닫힌 루프 서술**로 다시
쓰였다. `architecture.md`, `architecture-and-demo-flow.md`, `deployment.md`,
`system-guide-for-ops.md`, 루트·infra·dashboard `AGENTS.md`가 실행 스택을 서술한다.
`agent/0012`는 `Superseded by 0017`이다. R-1의 승격은 흐름·아키텍처·운영 문서 세 곳에
반영했다(아래 ADR 정합 항목 참조).

### 겹쳐 있던 High 발견사항 — 전부 닫힘

인계 문서가 이 전환과 겹친다고 지목한 H-16~H-20이 모두 `VERIFIED`다.
`docs/rca-remediation-high-findings.md`의 전체 상태가 `VERIFIED` (H-01~H-20).

### ADR↔코드 정합 — `/adr-sync` 완료 (2026-07-31)

27건 전량 deep 검증했다. **요구사항 값은 전부 코드와 일치**했다 — 유사도 0.86/0.7,
top-K 3, 신뢰도 0.8/0.3/0.9, beam 3, 재생성 2회, 시간 예산 20분, 보존 60일/90일,
그리고 데모의 풀 15·임계치 12·누수 20. Status도 전량 정확하다.

다만 이 전환이 남긴 **ADR↔코드 drift 2건**을 찾아 고쳤다.

- **infra/0005에 회고 개정본 아이템이 없었다.** 코드는 개정본을 별도 항목으로 쓰는데
  ADR의 키 설계에 그 항목이 없었다. 개정본은 실행 항목과 달리 **덮어쓰기**이고
  **엔진별로 분리**되며 **분석 산출물보다 우선**한다 — 셋 다 개발자가 임의로 바꿀 수
  없는 계약이라 ADR이 보유해야 한다. 같은 ADR의 세션 SK 표기(`SESSION` →
  `{engine}#SESSION`)도 코드에 맞췄다.
- **agent/0007이 리포트의 초안 표기와 승인 화면의 표기를 구분하지 않았다.** R-1로
  승격이 생기면서, 리포트 본문은 분석 시점의 초안을 고정하지만 **승인 화면은 현재
  값을 읽어야** 한다. 구분하지 않으면 승인 판단이 확정된 과거 시점을 근거로 삼는다.

부수적으로 `agent/0009`·`agent/0011`·`infra/0003`의 "과거에는 … 제거되었다" 진화
서술을 현재 상태 단정으로 정리했다(세 전환 모두 decision-log에 이미 수확돼 본문
중복이었다). 코드→ADR·ADR→PRD 역참조는 0건이다.

---

## 3. 남은 작업

**R-3(CSP) 하나만 열려 있다.** R-1·R-2·R-4는 닫혔다. 닫힌 항목도 무엇을 왜 고쳤는지
남기려 아래에 함께 둔다.

### ~~R-1 `verification_status`에 승격 경로가 없다~~ — 해결 (2026-07-31)

두 ADR이 일관되게 승격을 요구했으므로(0008의 "실행과 회고를 거친 뒤에야 검증된 절차가
된다", 0018의 결정 동인) ADR을 약화시키지 않고 **승격을 구현**했다. ADR 0018이 승격의
값·전이 방향·주체를 보유하고, 0008이 분석의 쓰기 제한과 병합의 보존 의무를 보유한다.
decision-log에 한 줄 남겼다.

구현 시점에 **결함이 하나가 아니라 셋**이었다.

1. 승격 자체가 없었다 — 해결된 실행의 회고가 갱신을 반영해도 상태가 초안으로 남았다.
2. 모델 출력이 이 값의 권위였다 — 회고 갱신안에 검증 상태가 담기면 검증 없이 병합됐다.
3. **승격이 살아남지 못하는 경로가 두 곳 있었다.** Strands의 보강 경로가 병합 결과를
   필드마다 조립하면서 이 값을 빠뜨렸고, 기록을 다시 읽는 로더도 재구성하지 않았다.
   둘 다 기본값 초안으로 흡수되어 오류를 내지 않으므로, 승격이 다음 보강이나 다음
   로드에서 조용히 취소됐다.

셋을 함께 닫았다. 회고가 유일한 승격 주체이고, 갱신 없는 회고도 승격하며(절차가 그대로
이슈를 해소했다면 그것이 가장 강한 검증이다), 회고 실패와 미해결 실행은 승격하지
않는다. 승격은 개정본과 검색 인덱스 양쪽에 함께 반영된다 — 다음 실행은 개정본을, 다음
RCA의 보강은 인덱스를 읽으므로 한쪽만 갱신하면 승격이 한 경로에서만 보인다. 대시보드는
원시 값 대신 초안/검증됨을 표시한다.

검증: 승격·비승격 경로, 모델의 승격·강등 시도 차단, 사본 오염 방지, 읽을 수 없는 기록
값의 초안 처리, 두 엔진의 상태 어휘 일치와 강등 연산 부재, 승인 화면이 개정본을 통해
상태를 읽는 것을 테스트로 고정했다. 수치는 1장 표를 참조한다.

### ~~R-2 실행·회고 경로의 라이브 E2E~~ — 해결 (2026-07-31, 4차 실측)

**네 차례 실측으로 결함 5건을 찾아 전부 고쳤고, 4차에서 닫힌 루프가 끝까지 돌았다** —
실행 → 해결 관측 → 회고 → 승격. 상세 기록은
[라이브 E2E 실측 보고서](./test-reports/playbook-execution-live-e2e-2026-07-31.md)가
보유한다.

| 항목 | 판정 | 성립 시점 |
|------|------|----------|
| R-2a 파괴적·판정 불가 명령 차단 | 성립 | 2차 |
| R-2b 재전달 중복 방지 | 성립 | 2차 |
| R-2c 회고 뒤 승격 | 성립 | 4차 |

**실측만이 찾을 수 있었던 결함 5건.** 다섯 모두 단위·계약 테스트를 전부 통과하는
상태에서 드러났다 — 테스트는 러너를 대역으로 바꾸므로 실제 CLI·이미지·IAM이 관여하는
구간은 검증 범위 밖이다.

1. **`rc=0` 경로의 응답 미기록** (1차, `039bd22`). 에이전트가 실패를 보고하지 않고 성공
   종료하면 원인을 사후에 읽을 방법이 없었다. 이 로깅이 이후 세 결함의 진단 근거가 됐다.
2. **실행 도구가 루트 에이전트에 있어 노출되지 않음** (1차, `f0295cb`). MCP 도구는
   `Agent(...)`로 위임된 서브 에이전트에서만 해석된다. 분석 경로가 동작하는 이유가 이
   차이였다 — 분석은 도구를 서브에 두고, 실행은 루트에 뒀다.
3. **실행 이미지에 `aws` CLI 부재** (2차, `bb43de9`). 게이트가 실행 파일을 `aws`로 못
   박고 argv를 셸 없이 spawn하는데 이미지가 그 바이너리를 담지 않았다. 분석은 MCP로
   AWS에 접근해 필요가 없었다.
4. **루트가 위임 결과를 기다리지 않고 종료** (3차, `b9bcc62`). 하위가 4절차를 모두
   수행했는데 루트가 배경 작업으로 띄우고 먼저 응답해, 마지막 해소 기록만 유실됐다.
   위임 구조(2번)가 도구 노출을 해결하며 함께 들여온 부작용이다.
5. **실행 역할에 `iam:PassRole` 부재** (3차, `b9bcc62`). 거부된 것은 `UpdateService`가
   아니라 PassRole이었다 — 태스크 정의를 지정하면 ECS가 그 정의의 역할을 넘겨받아야
   한다. `force-new-deployment`는 통과하고 롤백만 막혔다. ADR 0017이 롤링 배포를
   허용하고 infra/0008이 역할 정책을 "명백히 불필요한 범위 제외"로 한정하므로 결정
   변경이 아니라 정책이 결정을 구현하지 못한 것이었다. `ecs-tasks`로 한정해 허용했다.

**안전 경계는 네 차례 모두 무너지지 않았다.** 도구가 없을 때 에이전트는 수행하지 않은
것을 수행했다고 기록하기를 거부했고, 게이트는 모델이 합성한 파괴적 명령(`sts`)과 판정
불가 명령(`aws --version`)을 거부했으며 그 차단이 나머지 절차를 중단시키지 않았다.
조치가 실제로 수행됐는데 해소 기록이 없던 3차도 서버는 해결로 추정하지 않았다.

### R-3 대시보드 CSP — H-19의 명시적 잔여 위험

H-19는 raw HTML을 **생성하지 않는** 방향으로 닫혔고 CSP는 심층 방어로 남겼다.
`packages/dashboard`에 CSP 헤더 설정이 없다. 대시보드는 인증이 없는 로컬 전용
도구이므로 우선순위는 낮지만, 항목으로는 열려 있다.

### ~~R-4 문서 정정 두 건~~ — 해결 (2026-07-31)

- `docs/rca-remediation-high-findings.md`의 H-16 검증 줄이 두 엔진의 테스트 수치를
  바꿔 적은 것을 정정했다 (agent 502, cc-headless 486).
- 같은 문서 "공통 검증 기준"의 `reset` 전제 부정 테스트 목록을 현재 실행 안전 경계로
  대체했다 — 승인 없는 실행 금지, 파괴성·판정 불가 거부, 관측 없는 해결 금지, 미해결
  실행의 승격 금지.

---

## 4. 순서에 대한 판단

R-1을 먼저 처리했다. 계약의 공백이었고 ADR 판단이 선행돼야 했으므로 다른 항목과
병렬로 두면 결정이 흐려진다. 이 판단이 맞았던 이유는 승격을 구현하면서 **아무도
예상하지 않은 결함 두 개**(보강 경로와 로더의 상태 유실)가 같은 계약 안에서 드러났고,
뒤이은 `/adr-sync`가 **ADR 쪽 drift 두 건**을 더 드러냈기 때문이다. 초안 단일 상태로
확정하는 쪽을 골랐다면 이 넷 모두 발견되지 않았다.

R-2를 R-3보다 먼저 둔 판단의 값이 가장 크게 나왔다. **실측만이 찾을 수 있는 결함 5건이
드러났고, 단위·계약 테스트는 다섯 모두에서 전부 통과했다.** 테스트는 러너를 대역으로
바꾸므로 실제 CLI가 도구를 해석하는지, 이미지가 바이너리를 담았는지, 루트가 위임을
기다리는지, IAM이 롤백을 허용하는지는 검증 범위 밖이다.

결함이 순차로만 드러난 것도 실측을 네 번 돌려야 했던 이유다. **앞의 결함이 뒤의 결함을
가린다** — 도구가 없으면 이미지에 CLI가 없는 것을 알 수 없고, CLI가 없으면 루트가
기다리지 않는 것을 알 수 없고, 루트가 먼저 끝나면 PassRole이 없는 것이 판정에 드러나지
않는다. 각 차수가 다음 차수의 관측을 열었다.

찾은 결함마다 계약 테스트를 함께 붙였다 — 게이트 상수와 Dockerfile의 결속, 루트의 위임
대기, `iam:PassRole`의 `ecs-tasks` 한정. 실측으로만 드러나는 결함은 같은 방식으로 다시
드러나지 않게 고정하는 것이 유일한 방어다.

남은 것은 **R-3(CSP)** 하나이며 독립이고 우선순위가 가장 낮다 — 대시보드는 인증 없는
로컬 전용 도구이고 주 경로는 H-19에서 닫혔다.

**커밋 상태**: 11개 커밋으로 나눠 기록했다 — R-1 승격(계약 변경), adr-sync 정합(문서),
플레이북 화면 재작성, devtools 비활성화, 실행 응답 로깅, 실행 도구 위임 구조, 동작 방식
설명 문서, 라이브 실측 인계, 실행 이미지의 AWS CLI, 위임 대기와 PassRole, 실측 기록.

---

## 5. 확정된 설계 결정 (다시 논의하지 않음)

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

> ⚠️ `temperature`와 `effort`를 다시 넣으면 Bedrock Sonnet 5에서 **모든 LLM 호출이
> 실패**한다. 회귀 테스트(`TestSamplingParameters`,
> `test_never_declares_an_effort_level`)가 막고 있다.

---

## 6. 공통 검증

```bash
pnpm verify
pnpm --filter infra build
pnpm --filter dashboard build
```

안전 경계를 건드린 변경은 다음 부정 테스트를 반드시 포함한다.

- 사용자 승인 없이 실행이 시작되지 않는다.
- 파괴적 액션과 판정 불가 명령이 차단되고 증거에 남는다.
- 미확정 원인의 플레이북에 실행 절차가 없다.
- 관측으로 확정할 수 없는 결과가 해결로 기록되지 않는다.
- 미해결·실패한 실행이 회고로 플레이북을 갱신하지 않는다.
- 실행 실패가 분석 리포트를 변경하지 않는다.

인프라 계약을 바꾼 변경은 CDK synth를 추가한다.

---

## 7. 관련 문서

- [RCA에서 플레이북 실행까지](./rca-to-remediation-flow.md) — 닫힌 루프 전체 흐름
- [아키텍처](./architecture.md) — dual-stack, 파이프라인, 저장소
- [배포](./deployment.md) — 스택 목록, 실행 워커 배포
- [운영 가이드](./system-guide-for-ops.md) — 인프라·데모 운영
- [라이브 E2E 실측 절차](./execution-live-e2e-runbook.md) — 실행 경로를 다시 돌릴 때
- [라이브 E2E 실측 보고서](./test-reports/playbook-execution-live-e2e-2026-07-31.md) — 2·3·4차 결과
- [High 발견사항 추적](./rca-remediation-high-findings.md) — H-01~H-20 전부 `VERIFIED`
- ADR 인덱스: `docs/adr/.mapping.json`
