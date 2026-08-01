# RCA → 복구 루프 현황 점검 — 의도한 설계와 코드의 대조

- 점검일: 2026-08-01
- 점검 기준 커밋: `14137e4`
- 범위: `docs/adr/`, `packages/agent`, `packages/cc-headless`, `packages/infra`, `packages/dashboard`
- **상태: 갭 A·B·C 모두 수정 완료 (2026-08-01)** — 아래 §4 참조

> **점검 후 정정**: 최초 작성 시 갭을 A·B 둘로 봤으나, 수정에 착수하며 세 번째 갭(**갭 C**)을
> 찾았고 그것이 루프가 열린 실제 원인이었다. 회고 개정본을 **어느 엔진도** 읽지 않는
> 상태였고, 갭 B만 고치면 그 결함을 두 번째 경로로 복제하게 된다. 수정 순서를
> C → A → B 로 잡은 이유가 이것이다.

## 이 문서의 목적

"RCA → 리포트 1개(플레이북 포함) → 사용자 승인 → 별도 권한 에이전트가 실행 → 해결 판정
→ 회고가 절차 교정 → 대시보드에서 이슈·플레이북·증거·diff 대조" 라는 **의도한 설계**를
기준으로, 현재 코드가 그 설계를 어디까지 구현했고 어디가 비었는지 항목별로 대조한다.

결론부터: **의도한 흐름은 이미 ADR로 결정되어 있었고 코드도 대부분 그 결정을 구현하고
있었다.** 실행이 불명확하게 느껴진 원인은 설계 부재가 아니라, **갭 3건**이 흐름의 세
지점에서 루프를 끊어 놓은 데 있었다. 그 셋을 §4에서 고쳤다.

가장 큰 것은 갭 C였다 — 회고가 절차를 교정해도 **그 교정본을 다음 분석이 읽지 않았다.**
루프의 마지막 화살표가 그려져 있지만 연결되어 있지 않은 상태였고, 검색과 병합이 정상
동작하므로 로그에도 실패로 남지 않았다.

> 흐름 자체의 서술과 각 경계의 근거는
> [RCA에서 플레이북 실행까지](./rca-to-remediation-flow.md)와
> [이 시스템은 어떻게 동작하는가](./how-it-works.md)가 보유한다. 이 문서는 그 문서들이
> 서술한 것과 코드가 실제로 하는 것의 **차이만** 다룬다.

---

## 1. 요약 — 의도 대비 구현 상태

| # | 의도한 설계 | 결정 기록 | 점검 시점 | 현재 |
|---|-------------|-----------|-----------|------|
| 1 | RCA 후 분석 리포트가 **1개** 나온다 | agent/0007 | ✅ | ✅ |
| 2 | 그 리포트가 **플레이북을 포함**한다 | agent/0007, 0008 | ⚠️ 갭 A — Strands 분리 | ✅ 수정 |
| 3 | 기존 플레이북이 있으면 **참조해서** 만든다 | agent/0008 | ⚠️ 갭 B — CC 미구현 | ✅ 수정 |
| 3b | 참조 대상이 **회고가 교정한 절차**여야 한다 | agent/0008, 0018 | ⚠️ **갭 C** — 양쪽 원본만 읽음 | ✅ 수정 |
| 4 | 사용자가 대시보드에서 **명시적으로** 실행 | agent/0017, infra/0008 | ✅ | ✅ |
| 5 | 실행은 **별도 권한 에이전트**가 한다 | agent/0017, infra/0008 | ✅ | ✅ |
| 6 | 실행 에이전트가 해소를 **보고 있다가** 완료로 갱신 | agent/0017 | ✅ 판정 권위는 서버 | ✅ |
| 7 | 실패한 명령·에러를 **종합해 보유** | infra/0002, 0005 | ✅ | ✅ |
| 8 | 같은 실수를 반복하지 않도록 **회고가 플레이북 갱신** | agent/0018 | ✅ | ✅ |
| 9 | 회고에서 **이슈·플레이북·증거·diff** 4종 대조 | agent/0018 | ✅ | ✅ |
| 10 | 기본 모델을 **Sonnet 5**로 | agent/0010 | ✅ 이미 완료 | ✅ |

**ADR 본문은 갭 C 한 곳만 수정했다.** 요청한 흐름은 이미 ADR 0007·0008·0017·0018과
infra/0002·0005·0008이 결정으로 보유하고 있었고, 갭 A·B는 ADR이 결정한 바를 코드가
따라오지 못한 것이므로 코드만 고쳤다. 갭 C는 달랐다 — ADR 0008이 "상세를 로드한다"까지만
쓰고 **어떤 상세인지**를 비워 두었기에, 코드가 분석 원본을 읽어도 ADR 위반이 아니었다.
결정의 공백이므로 ADR을 먼저 고쳤다.

---

## 2. 현재 동작하는 전체 흐름

```mermaid
flowchart TB
    subgraph A["① 분석 — 자동 · 읽기 전용"]
        CW["CloudWatch 증상 알람"]
        SNS["SNS → SQS 팬아웃"]
        ST["Strands 엔진"]
        CC["CC Headless 엔진"]
        CW --> SNS
        SNS --> ST
        SNS --> CC
    end

    subgraph B["② 산출물 — 리포트 1개"]
        REP["report.md + playbook.json<br/>verification_status = DRAFT"]
    end

    subgraph C["③ 승인 — 사람"]
        UI["대시보드 리포트 화면<br/>절차 열람 + 초안/검증됨 표기"]
        BTN["POST /api/executions"]
        UI --> BTN
    end

    subgraph D["④ 실행 — 쓰기 권한 에이전트"]
        Q["실행 요청 큐<br/>이벤트 구독 없음"]
        WRK["execution_main<br/>execution-operator"]
        GATE["run_playbook_command<br/>argv 분해 → 파괴성 판정"]
        Q --> WRK --> GATE
    end

    subgraph E["⑤ 판정 — 서버"]
        EVD["실행 증거<br/>명령 · 인자 · 종료상태 · 실패분류 · 관측"]
        JDG["judge_resolution<br/>기록된 관측만 근거"]
        EVD --> JDG
    end

    subgraph F["⑥ 회고 — RESOLVED만"]
        RET["retrospective-analyst<br/>절차 결함만 교정"]
        MRG["merge_playbook_update<br/>삭제 불가"]
        PRM["promote_to_verified<br/>DRAFT → VERIFIED 단방향"]
        RET --> MRG --> PRM
    end

    ST --> REP
    CC --> REP
    REP --> UI
    BTN --> Q
    GATE --> EVD
    JDG -->|"RESOLVED"| RET
    JDG -->|"UNRESOLVED · FAILED"| STOP["증거 보존 · 자동 교정 없음"]
    PRM -->|"같은 playbook_id 로 개정본"| REP

    style C fill:#fff4e6,stroke:#f59e0b,stroke-width:2px
    style A fill:#eff6ff,stroke:#3b82f6
    style D fill:#f0fdf4,stroke:#22c55e
```

---

## 3. 항목별 대조

### 3.1 리포트가 1개 나오는가 — ✅

두 엔진 모두 분석을 리포트 하나로 끝내고 멈춘다. 분석 완료 알림은 사람과 대시보드만
수신하며 어떤 기계 동작도 트리거하지 않는다(ADR agent/0009).

- CC: `packages/cc-headless/src/cc_headless/services/pipeline.py:240` 이후 —
  `validate_completion_artifacts` 통과 → 플레이북 저장 → 리포트 저장 → 알림 → 완료
- Strands: `packages/agent/src/rca_agent/services/pipeline.py:1071` 이후 — 리포트 저장 →
  플레이북 생성 → 알림 → 완료

### 3.2 리포트가 플레이북을 포함하는가 — ⚠️ 갭 A

**CC Headless는 계약으로 강제한다.** `report.md`의 `## 대응 플레이북` 서술과
`playbook.json`의 `execution_steps`가 **같은 `step_id`를 같은 순서로** 담는지 완료 게이트가
검사하고, 어긋나면 저장을 거부한다
(`packages/cc-headless/src/cc_headless/services/artifact_validation.py:383` 이후).

**Strands는 이 계약이 없다.** 리포트 마크다운 렌더러가 `RcaReport` 필드만 직렬화하고
`execution_steps`를 렌더하지 않는다
(`packages/agent/src/rca_agent/adapters/secondary/report/s3_report_store.py:154`,
`RcaReport` DTO는 `packages/agent/src/rca_agent/ports/dto/models.py:280`).

결과적으로 Strands 세션에서는:

- 리포트를 저장한 **뒤에** 플레이북을 만든다 → 리포트가 플레이북을 참조할 수 없다.
- 사람이 리포트 본문에서 승인할 절차를 읽을 수 없고, 대시보드가 플레이북을 별도 API로
  따로 불러와 나란히 보여주는 것으로 메꾼다.
- **승인한 서술과 실행할 구조의 일치가 보증되지 않는다** — ADR agent/0007이 두 표현의
  일치를 저장 조건으로 둔 이유가 여기서 무력화된다.

> 이 갭은 데모 실측에서도 드러났다. Strands가 전 가설을 기각하면 `execution_steps: 0`인
> 리포트가 나오고, 승인 대상 확보가 실측의 병목이 된다
> ([라이브 E2E 런북](./execution-live-e2e-runbook.md) 1장).

### 3.3 기존 플레이북을 참조해서 만드는가 — ⚠️ 갭 B

**Strands는 Search-First로 구현되어 있다.** 유사도 0.86 이상의 기존 플레이북을 찾고,
상세를 로드해 보강 여부를 판단하고, 보강 시 **같은 `playbook_id`를 유지**한다. 상세 로드가
실패하면 병합을 포기하고 신규 생성으로 떨어진다
(`packages/agent/src/rca_agent/services/playbook_gen.py:176` 이후).

**CC Headless에는 이 경로가 없다.** 플레이북 스토어 포트가 두 메서드만 노출한다:

```
# packages/cc-headless/src/cc_headless/ports/interfaces/playbook_store.py
load_playbook(artifact_dir)          # 산출물 파일 읽기
save_to_s3_vectors(playbook, ...)    # 인덱스에 쓰기
```

`search_similar`도 `load_detail`도 없고, Report 전문 에이전트에게 기존 플레이북을 전달하는
도구도 프롬프트도 없다. 즉 **CC 엔진은 같은 증상이 재발할 때마다 플레이북을 새로
만든다.** 새 `playbook_id`가 생기므로 축적이 갈라지고, 회고가 쌓아 온 검증된 절차가 다음
분석에서 참조되지 않는다.

이것이 루프를 끊는 지점이다. 회고는 개정본과 인덱스 양쪽을 갱신하지만
(`execution_pipeline.py`의 `_publish_playbook`), CC 분석이 인덱스를 **읽지 않으므로**
승격된 절차가 CC 경로에서는 다음 분석으로 돌아오지 않는다.

### 3.4 사용자가 명시적으로 실행하는가 — ✅

승인 게이트가 코드 조건이 아니라 **경로의 성질**로 서 있다.

- 실행 스택에 SNS 구독도 이벤트 규칙도 없고, 유일한 입구가 대시보드만 발행하는 큐다
  (`packages/infra/lib/stacks/playbook-execution-stack.ts`).
- 워커가 메시지에 `AlarmName`이나 `Trigger`가 있으면 실행 요청으로 해석하지 않는다
  (`services/execution_request.py`).
- 승인 API가 분석 세션이 `COMPLETED`가 아니면 거부하고, 실행 식별자를 요청 내용에서
  결정론적으로 파생해 재전달 중복을 claim으로 걸러낸다.
- 대시보드가 절차가 **초안인지 검증됨인지** 표기해 승인 판단의 근거로 삼게 한다
  (`packages/dashboard/app/pages/report/[id].vue:245`).

### 3.5 별도 권한 에이전트가 실행하는가 — ✅

| | 분석 | 실행 |
|---|------|------|
| 진입점 | `main.py` | `python -m cc_headless.execution_main` |
| 이벤트 구독 | 있음(알람) | **없음** |
| 쓰기 권한 | 없음 | 있음 — 시스템에서 유일한 쓰기 태스크 역할 |
| 하네스 | `.claude/` (읽기 전용 도구) | `.claude-execution/` (쓰기 도구) |

같은 컨테이너 이미지를 다른 진입점으로 실행하므로 실행 도구와 분석 도구가 같은 프로세스에
들어가는 일이 없다. 하네스 계약 테스트가 쓰기 도구의 혼입을 막는다
(`packages/cc-headless/tests/test_execution_harness_contracts.py`).

실행 에이전트의 쓰기 경로는 세 도구뿐이다 — `run_playbook_command`,
`record_step_outcome`, `record_resolution`. Bash도 임의 HTTP도 없고, 명령은 서버가 argv로
분해해 파괴성을 판정한 뒤에만 실행된다
(`packages/cc-headless/src/cc_headless/adapters/secondary/cc/cc_execution_runner.py:52` 이후,
판정은 `services/command_gate.py`).

### 3.6 해소를 보고 있다가 완료로 갱신하는가 — ✅ (판정 주체는 서버)

요청한 "실행 에이전트가 이슈 해결 여부를 보고 있다가 상태를 완료로 갱신"은
**에이전트가 관측하고, 서버가 판정한다**로 구현되어 있다. 에이전트는
`record_step_outcome`·`record_resolution`으로 관측을 기록하고, 상태 확정은
`judge_resolution`이 한다(`services/execution_outcome.py`).

해결로 전이하는 조건 셋이 모두 성립할 때만 `RESOLVED`다.

1. 실행 에이전트가 정상 종료했다.
2. 해소가 관측으로 확인되었다.
3. 시도된 절차 중 성공 기준 미달도, 관측 없는 것도 없다.

**관측 기록이 아예 없으면 `UNRESOLVED`다.** 모델이 "정상화되었습니다"라고 서술하는 것은
판정에 들어가지 않는다 — 이것을 근거로 삼으면 해소되지 않은 장애가 완료로 기록되고, 그
절차가 회고를 거쳐 검증됨으로 승격된다. 상태 기계에도 `EXECUTING → RESOLVED` 직접 전이가
없어 반드시 `VERIFYING`을 거친다.

### 3.7 실패한 명령과 에러를 종합해 보유하는가 — ✅

증거가 명령 단위로 누적되고, 절차 목록을 **플레이북 기준으로** 조립하므로 에이전트가
언급하지 않은 절차도 증거에 나타난다(시도되지 않은 절차가 조용히 사라지지 않는다).

| 기록 | 회고에서 쓰이는 방식 |
|------|---------------------|
| `step_id` | 교정을 붙일 위치 |
| 명령과 인자 | 잘못 부른 인자 특정 |
| 종료 상태·오류 출력 | 실패 분류의 근거 |
| 실패 분류 | 인자 오류 / 권한 부족 / 리소스 부재 / 일시 오류 / 거부 |
| 재시도·교정 내역 | 무엇으로 바꿔 성공했는지 = 절차에 넣을 정답 |
| 관측 결과 | 해결 판정의 유일한 근거 |

실패한 실행에서도 증거는 보존되고, 자격 증명으로 보이는 인자는 가린다(`redact`).
주 보관소는 S3, DynamoDB에는 요약만 둔다.

### 3.8 회고가 같은 실수를 막도록 플레이북을 갱신하는가 — ✅

**교정 대상은 절차의 결함으로 환원되는 실패뿐이다.** 잘못된 인자, 누락된 인자, 빠진 선행
조건, 순서 오류, 해결 확정에 필요했던 검증 절차. `failure_class`가
`TRANSIENT`·`THROTTLED`·`TIMEOUT`·`UNKNOWN`인 실패는 교정하지 않는다 — 재시도로 성공했다면
절차 자체는 옳았고, 일시 실패를 절차 결함으로 처리하면 불필요한 우회가 쌓인다.

**삭제 불가는 프롬프트 지시가 아니라 병합 코드의 성질이다**
(`services/playbook_merge.py`).

- 갱신안이 담지 않은 필드는 기존 값 유지
- 갱신안에 없는 절차는 그대로 남음
- `step_id`와 순서는 살아남고 새 절차는 뒤에 붙음
- 관측 가능한 성공 기준이 없는 새 절차는 거부
- 갱신안을 해석할 수 없으면 아무것도 바꾸지 않음

**승격은 단방향이다.** `DRAFT → VERIFIED`만 있고 되돌아오지 않으며, 이 값은 서버가
소유한다 — 회고 모델이 갱신안에 담아도 병합이 버린다. 교정할 결함이 없어 갱신이 비어
있어도 승격은 일어난다(절차가 그대로 이슈를 해소한 것이 가장 강한 검증).

실행 시작 시점에 **갱신 전 플레이북 사본을 S3에 보존**한다 — 회고가 원본을 덮어쓰므로
사본이 없으면 diff의 기준이 사라진다.

`UNRESOLVED`·`FAILED`·`CANCELLED`는 회고에 들어가지 않는다. 이슈를 해소하지 못한 절차는
올바름이 입증되지 않았고, 그 증거로 절차를 고치면 근거가 "이렇게 했더니 해결되지 않았다"가
된다. 증거는 보존되고 사람이 읽을 수 있다 — **자동으로 고치지 않는다**는 뜻이지 버린다는
뜻이 아니다.

### 3.9 이슈·플레이북·증거·diff 4종을 대조할 수 있는가 — ✅

전용 화면이 있고, API가 네 가지를 한 응답으로 조립한다
(`packages/dashboard/server/api/retrospectives/[rcaId]/[executionId].get.ts` →
`packages/dashboard/app/pages/retrospective/[rcaId]/[executionId].vue`).

| # | 내용 | 출처 |
|---|------|------|
| 1 | 이슈 | 세션 아이템 — 알람명, 근본원인, 확정 여부, 엔진 |
| 2 | 실행 전 플레이북 | 실행 시작 시점 S3 스냅샷 |
| 3 | 실행 증거 | 절차별 시도·명령·인자·오류·관측 |
| 4 | 갱신 diff | 교정된 절차의 필드별 before/after + 추가 절차 + 근거 |

`playbookAfter`(개정본)도 함께 반환한다. 진입 경로는 세션 목록의 "회고 반영" 배지
(`app/pages/index.vue:434`)와 리포트 상세의 실행 이력
(`app/pages/report/[id].vue:357`) 두 곳이다.

정리된 객체가 있으면 그 칸만 비워 보여주고 요청 전체를 실패시키지 않는다 — 네 가지 중
하나가 없다는 사실 자체가 사람이 알아야 할 정보다.

### 3.10 기본 모델이 Sonnet 5인가 — ✅ 이미 완료

**변경할 것이 없다.** 두 엔진 모두 이미 Sonnet 5 세대를 기본값으로 쓴다.

| 대상 | 설정 위치 | 값 |
|------|-----------|-----|
| Strands (로컬) | `packages/agent/src/rca_agent/config/settings.py:10` | `global.anthropic.claude-sonnet-5` |
| Strands (env) | `packages/agent/env/local.env:2` | 동일 |
| CC 분석 워커 | `packages/infra/lib/stacks/cc-headless-stack.ts:122` | `ANTHROPIC_DEFAULT_SONNET_MODEL` = 동일 |
| CC 실행 워커 | `packages/infra/lib/stacks/playbook-execution-stack.ts:127` | 동일 |

`packages/agent/tests/test_agent_factory.py`가 기본값을 테스트로 고정하고 있고, ADR
agent/0010이 "기본 모델은 Claude Sonnet 5 세대"를 결정으로 보유한다. 도입 커밋은
`9030937 feat(agent,infra): move the default model to the Sonnet 5 generation`.

이 세대의 호출 표면 제약 두 가지도 코드에 반영되어 있다(`agent_factory.py`):
`temperature`를 전달하지 않고, 사고량은 adaptive 여부로만 제어하며 `effort`를 보내지
않는다. 실행 워커는 Claude Code CLI를 Bedrock으로 붙이므로(`CLAUDE_CODE_USE_BEDROCK=1`)
모델 지정이 환경변수 한 곳에 모인다.

---

## 4. 갭 3건 — 무엇이 문제였고 어떻게 고쳤는가

세 갭은 **같은 뿌리**를 갖는다. CC와 Strands가 플레이북을 다루는 층위가 다르다 — CC는
플레이북을 리포트 산출물 계약의 일부로 두고, Strands는 리포트와 별개의 파이프라인 단계로
둔다. 그래서 CC에는 리포트-플레이북 일치 검사가 있고 축적 검색이 없으며, Strands에는
축적 검색이 있고 일치 검사가 없었다. 그리고 **어느 쪽도 회고 개정본을 읽지 않았다.**

### 갭 C — 회고 개정본을 분석이 읽지 않았다 (최우선, 루프가 열린 실제 원인)

**증상**: Strands의 상세 로드가 `span_type = PLAYBOOK` 필터로 조회했다. 회고 개정본은
`SK = "{engine}#PLAYBOOK_REVISION"` 아이템이고 `span_type` 속성이 **없으므로**, 그 필터가
개정본을 구조적으로 배제했다. 검색 경로가 있어 되는 것처럼 보였지만 로드하는 것은 회고가
교정하기 **전의** 원본이었다.

**왜 문제였나**: 병합이 모든 필드를 보존해도 결과가 교정 이전으로 퇴행한다. 검색과 병합이
정상 동작하므로 로그에도 실패로 남지 않고, 같은 장애가 반복될수록 회고가 매번 같은 교정을
다시 하게 되어 축적이 제자리에 머문다. 실행 워커와 승인 화면은 이미 개정본을 현재 절차로
판별하고 있었으므로, **분석 경로만 다른 문서를 읽는** 상태였다.

**고친 내용**:

- 상세 조회가 회고 개정본을 우선 조회하고, 없을 때만 분석 스팬으로 떨어진다. 파티션 쿼리
  한 번으로 둘을 함께 읽으므로 조회 횟수는 늘지 않는다.
- 개정본을 해석할 수 없으면 원본으로 떨어진다 — 그것을 병합 포기 사유로 삼으면 아직 병합
  가능한 플레이북이 버려지고 같은 유형이 새 식별자로 다시 생성된다.
- 벡터 인덱스 메타데이터에 `verification_status`를 담아, 상세를 로드하지 않고도 검색
  결과에서 승격 여부가 보인다. ADR 0008 결정사항 4의 "최소 필드"에 이 값을 포함시켰다.
- ADR 0008에 "보강 대상 상세는 회고 개정본 우선"을 명시하고 decision-log에 기록했다.

**고정한 계약**: 개정본 우선, 승격이 재로드를 견딤, 개정본 없으면 원본으로 폴백, 해석 불가
시 폴백, 다른 플레이북의 개정본은 무시.

### 갭 A — Strands 리포트가 실행 절차를 담지 않았다

**증상**: 리포트 마크다운에 `## 대응 플레이북` 섹션이 없었고, 리포트를 저장한 **뒤에**
플레이북을 만들었다. 승인 화면이 리포트 본문과 별도 API의 플레이북을 나란히 놓는 것으로
메꾸고 있었으며, 두 표현의 `step_id` 일치가 저장 조건으로 검사되지 않았다.

**왜 문제였나**: 사람은 서술을 보고 승인하는데 실행은 구조를 따라간다. 승인한 내용과
실행한 내용이 다르면 승인 게이트는 형식만 남는다.

**고친 내용**:

- 파이프라인 순서를 반전했다 — 플레이북을 먼저 만들고 리포트에 담는다. 순서가 반대면
  리포트는 자신이 담아야 할 절차를 모르는 상태로 확정된다.
- 리포트 저장에 플레이북을 **필수 인자**로 요구한다. 선택 인자로 두면 절차 없는 리포트가
  승인 버튼 뒤에 놓이는 경로가 남는다.
- 렌더러가 `## 대응 플레이북` 섹션에 각 절차의 `step_id`·의도·작업·성공 기준을 쓰고,
  초안임을 본문에 고정 표기한다.
- 확정 원인이 없으면 절차 대신 "왜 절차를 만들지 않았는지"를 쓴다.
- `step_id` 집합과 **순서**가 두 표현에서 같은지 검사하고, 어긋나면 저장을 거부한다.
- 프로덕션 호출처가 없던 중복 렌더러·저장 함수(`services/report.py`)를 제거했다. 남겨
  두면 일치 검사를 우회해 플레이북 없는 리포트를 쓰는 경로가 된다.

### 갭 B — CC 분석이 기존 플레이북을 참조하지 않았다

**증상**: CC의 플레이북 스토어 포트에 `search_similar`도 `load_detail`도 없었다. 같은
증상이 재발하면 매번 새 `playbook_id`가 생겨 축적이 갈라졌다.

**고친 내용**:

- 포트에 `search_similar(query_text, *, threshold)`와 `load_detail(match)`을 추가했다.
  상세 조회는 갭 C와 같은 우선순위(개정본 우선)를 따른다.
- 병합 임계값은 ADR이 고정한 **0.86**을 쓴다. 일반 조회보다 엄격한 이유는 다른 유형의
  장애를 같은 플레이북으로 병합하면 절차가 뒤섞여 어느 쪽에도 쓸 수 없어지기 때문이다.
- **임베딩 템플릿을 두 엔진이 공유한다.** 한쪽이 필드를 다르게 자르거나 라벨을 다르게
  붙이면 임베딩 공간이 갈라져 같은 증상의 플레이북이 서로를 찾지 못한다.
- 저장과 검색의 **입력 유형을 분리했다**(`search_document` / `search_query`). 한 유형으로
  양쪽을 처리하면 같은 장애의 저장 벡터와 검색 벡터가 어긋나 유사도가 낮게 나온다.
- 병합은 회고가 쓰는 `merge_playbook_update`를 재사용한다 — 삭제 불가 규칙이 이미 코드에
  있으므로 분석 보강에도 같은 규칙이 적용된다.
- 상세를 읽지 못한 후보는 병합 대상에서 **제외**하고 신규 생성으로 떨어진다.
- 병합 결과의 `playbook_id`와 `verification_status`는 기존 플레이북 값을 유지한다 —
  보강 한 번이 회고의 승격을 취소하지 않게 한다.
- 검색·병합 실패는 분석을 중단시키지 않는다. 플레이북은 미래를 위한 자산이고 이번 RCA의
  결과물은 리포트다.

### 갭이 아닌 것 — 판정 주체

요청 문구는 "실행 에이전트가 해결되면 상태를 완료로 업데이트"였지만 구현은 **서버가
판정**한다. 이것은 갭이 아니라 의도된 강화이므로 그대로 두었다. 에이전트의 닫는 요약은
관측이 아니고, 그것을 근거로 삼으면 해소되지 않은 장애가 완료로 기록된 뒤 회고를 거쳐
검증된 절차로 승격된다. 잘못된 절차가 다음 장애의 근거가 되므로 오류가 한 번의 오판으로
끝나지 않는다.

---

## 5. 검증 결과

| 대상 | 결과 |
|------|------|
| `packages/agent` 테스트 | **520 passed** (기존 516 + 신규 계약 테스트) |
| `packages/cc-headless` 테스트 | **534 passed** (기존 508 + 신규 계약 테스트) |
| `packages/infra` 빌드·테스트 | **31 passed** |
| `packages/dashboard` 빌드 | 성공 |
| Ruff (두 패키지) | All checks passed |

신규 계약 테스트가 고정하는 것:

- 상세 조회가 **개정본을 우선**하고, 승격이 재로드를 견디고, 개정본이 없거나 해석 불가면
  원본으로 폴백한다 (양쪽 엔진).
- 리포트가 절차를 렌더하고, 절차가 누락되거나 **순서가 다르면 저장을 거부**한다.
- CC 병합이 기존 식별자를 유지하고, 축적된 절차와 새 분석이 언급하지 않은 필드를 보존하고,
  **획득한 검증 상태를 낮추지 않는다**.
- 상세를 읽지 못한 후보는 건너뛰고, 검색 실패가 분석을 막지 않는다.
- 저장과 검색이 **다른 입력 유형**을 쓴다.

### 남은 검증 — 라이브

오프라인 계약은 전부 통과했지만, 루프가 실제로 닫히는지는 라이브에서만 확인된다. 확인할
것은 하나다: **CC 엔진에서 같은 증상을 두 번 주입했을 때, 두 번째 분석이 첫 회고가 승격한
절차를 참조하는가.** 절차는 [실행 E2E 런북](./execution-live-e2e-runbook.md)을 따른다.

> 배포 전 확인: 배포된 이미지가 현재 코드인지 먼저 대조한다. 태그가 HEAD보다 앞서 있어도
> 해당 워커의 빌드 컨텍스트에 변경이 없으면 이미지 내용은 같다(런북 0장).
>
> 이번 변경은 **양쪽 분석 워커를 모두 건드렸다.** `agent`와 `cc-headless` 둘 다
> 재배포해야 한다.

## 6. 관련 문서

| 문서 | 내용 |
|------|------|
| [RCA에서 플레이북 실행까지](./rca-to-remediation-flow.md) | 흐름의 각 경계와 그 경계가 왜 그 자리에 있는지 |
| [이 시스템은 어떻게 동작하는가](./how-it-works.md) | 처음 읽는 사람을 위한 동작 설명 |
| [실행 E2E 런북](./execution-live-e2e-runbook.md) | 승인 이후 구간을 실환경에서 돌리는 절차 |
| [High 발견사항 추적](./rca-remediation-high-findings.md) | H-01~H-20 (전부 해결됨) |
| [ADR agent/0007](./adr/agent/0007-rca-report-generation.md) | 플레이북을 포함한 단일 리포트 |
| [ADR agent/0008](./adr/agent/0008-playbook-generation.md) | Search-First 병합과 실행 절차 요구사항 |
| [ADR agent/0010](./adr/agent/0010-model-tier-architecture.md) | Sonnet 5 단일 모델 + Planning/Execution 분리 |
| [ADR agent/0017](./adr/agent/0017-playbook-execution-agent.md) | 승인 게이트와 파괴적 액션 차단 |
| [ADR agent/0018](./adr/agent/0018-playbook-retrospective.md) | 회고의 교정 범위와 단방향 승격 |
| [ADR infra/0008](./adr/infra/0008-playbook-execution-stack.md) | 쓰기 권한 전용 실행 스택 |
