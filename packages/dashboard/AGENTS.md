# AGENTS.md

> 이 패키지는 RCA Agent 모노레포의 일부입니다. 전체 아키텍처, ADR, 크로스 패키지 계약, 빌드 명령어는 **[루트 AGENTS.md](../../AGENTS.md)** 를 참조하세요.

## Project Overview

RCA 대시보드는 DynamoDB 세션 상태와 S3 보고서를 조회하는 로컬 전용 웹 대시보드다. 인증 없이 로컬 AWS 크레덴셜(`~/.aws`)을 사용하며, 배포 대상이 아닌 개발/데모용 도구이다.

**이 대시보드는 플레이북 실행 승인의 유일한 진입점이다.** 실행 워커는 이 대시보드가 발행한
요청만 소비하며 이벤트 구독을 갖지 않으므로, 승인 없이 실행이 시작될 경로가 존재하지 않는다.
실행 승인과 플레이북 지식 제안의 반영·기각은 별개 쓰기 동작이며 로컬 자격 증명에 종속된다.
지식 반영은 기존 사고의 런북·보고서·승인 사본을 바꾸지 않는다.

### Core Features

- **세션 목록 조회**: 엔진별 시간 역순 인덱스에서 한 페이지씩 읽어 시간순 기록으로 표시하고,
  스크롤이 끝에 닿으면 커서로 다음 페이지를 잇는다. 승인을 기다리는 건수는 전체 기준 집계에서
  오고, 각 세션의 소요시간은 막대 길이로 보여준다
- **보고서 조회**: S3에 저장된 Markdown 보고서를 렌더링
- **요약과 상세 모달**: 보고서 첫 화면은 요약·짧은 5 Whys·다음 조치를 보여준다. 전문·원인 단계의 전체 근거·분석 경로·증거·런북·생성 참고 자료·실행 이력·회고는 한 모달의 내용을 전환해 읽으며, 내부 상세 페이지 이동이나 중첩 모달을 만들지 않는다. 현재 런북 전체 검토와 실행 승인도 같은 모달에서 수행한다. 각 조회의 실패와 재시도는 독립적이다.
- **생성에 사용한 참고 자료**: `used_references`에 기록된 역할·ref·원본 RCA·엔진·개정본을 표시한다. 재사용 지식은 고정 `baseline.playbook`, 런북 입력은 `inputs.current_report` 또는 `inputs.analysis`, 인용 근거는 같은 ref의 모든 `evidence` 관측값으로 연결한다. 검토만 한 검색 후보를 생성 출처로 표시하지 않으며, 실제 사용 목록이 없는 과거 기록은 사용 여부 미기록으로 구분한다.
- **독립 플레이북 라이브러리**: `/playbooks`는 STATE 요약 → STATE 없는 기존 HEAD → 레거시 벡터 순서로 제한된 페이지를 조회한다. 유형·태그·검증 상태로 필터링하고 ID를 중복 표시하지 않는다. 빈 페이지도 커서가 있으면 다음 페이지를 읽는다. 게시 대기·원문 누락은 조회 불가 사유로 표시하고 비교 대상으로 사용하지 않으며 조회 중 마이그레이션 쓰기를 하지 않는다.
- **지식 제안 처리**: 완료 사고에 고정된 비교 기록을 읽고 제안을 반영하거나 기각한다. 반영은 기준 개정본·전체 원문·완료 원본을 조건부 검사하며, 동일 트랜잭션에서 현재 지식·불변 사본·처리 결과를 확정한다. 검색 게시 실패 후에는 같은 요청으로 고정 개정본의 게시만 재시도한다.
- **플레이북 조회**: 세션이 가리키는 정확한 플레이북을 DynamoDB에서 조회하여 렌더링. 유형별 메트릭·판단 기준·대응 지식과 사고별 런북(`step_id`·의도·작업·구체 명령 또는 고정 사후 관측·성공 판정), 초안/검증됨 상태, 회고 개정 여부를 함께 보여준다. 회고 개정본은 세션의 현재 `playbook_id`와 일치할 때만 우선한다
- **트레이스 그래프**: DynamoDB 실행 트레이스를 Vue Flow 기반 DAG로 시각화 (가설 노드 + 스팬 노드)
- **파이프라인 상태 그래프**: Vue Flow 기반 상태 전이 그래프로 현재 파이프라인 진행 상황 표시
- **증거 상세 조회**: 가설별 S3에 저장된 full evidence를 on-demand로 조회
- **세션 취소/삭제**: 진행 중인 세션 취소(CANCELLED) 및 세션 삭제 지원. 취소는 claim을 회전시켜 실행 중 워커를 fencing하고, 삭제는 최종 상태이며 활성 lease·실행·해당 사고가 소유한 게시 대기가 없을 때만 허용한다
- **엔진 구분**: Strands / Headless Codex 엔진별 세션 필터링. 기존
  `codex-headless`·`cc-headless` 세션은 읽기·승인 호환 대상으로 유지
- **결과 한 단어**: 분석과 실행 두 생명주기를 인시던트 하나의 결말로 합쳐 표시한다 —
  승인 대기 · 해결 · 미해결 · 원인 미확정 · 분석 중단 · 건너뜀. `COMPLETED`만으로는
  승인이 필요한 리포트와 이미 처리된 리포트가 구별되지 않는다
- **실행 승인**: 리포트 안에서 현재 사고의 런북 명령·대상·순서를 읽고 승인 → 승인 시점 플레이북을 S3에 고정하고 DynamoDB에 `PENDING_APPROVAL` 실행과 `EXEC_ACTIVE`를 원자적으로 예약한 뒤 실행 요청 큐 발행
- **실행 이력**: 리포트별 실행 시도, 차단·실패 건수, 종료 상태 조회 (실패한 실행의 증거도 보존)
- **회고 4단 비교**: 이슈 · 실행 전 플레이북 · 실행 증거 · 갱신 diff 를 함께 조회

### Tech Stack

- **Framework**: Nuxt.js 4 (Vue 3)
- **UI**: TailwindCSS 4 + DaisyUI 5
- **Graph**: @vue-flow/core + @dagrejs/dagre
- **Markdown**: marked
- **Language**: TypeScript
- **Package Manager**: pnpm (Nx workspace)
- **AWS SDK**: @aws-sdk/client-dynamodb, @aws-sdk/client-s3, @aws-sdk/lib-dynamodb

## Quick Start

```bash
pnpm install
pnpm dev   # http://localhost:3100
```

## Project Structure

```
packages/dashboard/
├── app/
│   ├── pages/
│   │   ├── index.vue              # 운영 지표 + 결과별 필터 + 인시던트 큐 + 승인 진입
│   │   ├── report/[id].vue        # 보고서 상세 + 실행 절차 + 승인 게이트
│   │   ├── playbooks.vue          # 독립 라이브러리 목록 + 같은 페이지 상세 모달
│   │   ├── playbook/[id].vue      # 플레이북 상세 + 실행 절차 + 초안/검증됨
│   │   ├── retrospective/[rcaId]/[executionId].vue  # 회고 4단 비교
│   │   └── trace/[id].vue         # 시도한 가설 목록 + 파이프라인 그래프(접힘)
│   ├── layouts/
│   │   └── default.vue            # 장애 기록 레이아웃 (ledger / ledger-night 전환)
│   ├── components/
│   │   ├── CausalChain.vue        # 5 Whys를 하나의 선형 하강으로 렌더
│   │   ├── DetailDialog.vue       # 상세 전환·포커스·스크롤 복귀를 담당하는 단일 모달
│   │   ├── PlaybookComparison.vue # 실제 사용 참고 자료의 고정 원문 + 변경 제안 처리
│   │   ├── PlaybookKnowledge.vue  # 유형별 대응 지식
│   │   ├── AnalysisDetails.vue    # 분석 경로 + 가설별 증거 진입
│   │   ├── RetrospectiveDetails.vue # 같은 모달에서 실행 증거·회고 열람
│   │   ├── ReadState.vue          # 데이터별 로딩·오류·재시도
│   │   ├── StateGraph.vue         # 파이프라인 상태 전이 그래프 (Vue Flow)
│   │   └── flow/
│   │       ├── HypoNode.vue       # 가설 노드 커스텀 컴포넌트
│   │       └── SpanNode.vue       # 스팬 노드 커스텀 컴포넌트
│   ├── composables/
│   │   └── useTraceGraph.ts       # 트레이스 데이터 → Vue Flow 그래프 변환
│   ├── utils/
│   │   ├── markdown.ts            # 신뢰할 수 없는 Markdown 렌더 (raw HTML 미생성)
│   │   ├── causalChain.ts         # 리포트 산문 → 5 Whys 사슬 · 타임라인 (방어적 파싱)
│   │   └── sessionState.ts        # 상태·결과 어휘 + 엔진별 트랙 + 중단 지점 문구
│   ├── assets/css/main.css        # 운영 콘솔 디자인 토큰 + 커스텀 DaisyUI 테마 2종
│   └── app.vue                    # 루트 컴포넌트
├── server/
│   ├── api/
│   │   ├── sessions.get.ts        # GET /api/sessions — 세션 인덱스 1페이지 + 커서
│   │   ├── sessions-summary.get.ts # GET /api/sessions-summary — 전체 기준 집계
│   │   ├── sessions/
│   │   │   ├── [id]/index.get.ts  # GET /api/sessions/:id — 세션 1건
│   │   │   ├── [id].delete.ts     # DELETE /api/sessions/:id — 세션 삭제
│   │   │   └── [id]/
│   │   │       └── cancel.post.ts # POST /api/sessions/:id/cancel — 세션 취소
│   │   ├── executions.post.ts     # POST /api/executions — 실행 승인 발행 (유일한 진입점)
│   │   ├── executions/[rcaId].get.ts  # GET /api/executions/:rcaId — 실행 시도 이력
│   │   ├── retrospectives/[rcaId]/[executionId].get.ts  # 회고 4단 조회
│   │   ├── reports/[id].get.ts    # GET /api/reports/:id — S3 보고서 조회
│   │   ├── playbooks/[id].get.ts  # GET /api/playbooks/:id — DynamoDB 플레이북 조회
│   │   ├── playbook-library/      # 독립 목록·보존 원문 조회
│   │   ├── playbook-proposals/    # 고정 생성 참고 자료 조회·지식 반영/기각
│   │   ├── evidence/
│   │   │   └── [rcaId]/
│   │   │       └── [hypothesisId].get.ts  # GET /api/evidence/:rcaId/:hypothesisId — S3 증거 조회
│   │   └── traces/[id].get.ts     # GET /api/traces/:id — DynamoDB 트레이스 조회
│   └── utils/                     # Nitro 자동 임포트 (server/api 에서 import 없이 사용)
│       ├── aws.ts                 # DynamoDB/S3/SQS 클라이언트 싱글톤
│       ├── keys.ts                # DynamoDB 키 레이아웃 (PK/SK 조립·엔진 판별)
│       ├── execution.ts           # EXEC# 항목 → 실행 상태·요약 정규화
│       ├── progress.ts            # 스팬 → 도달한 최종 단계 (중단 지점 판정)
│       ├── readiness.ts           # 완료된 분석이 승인 대기인지 판정
│       ├── playbook.ts            # 세션 포인터 기반 현재 플레이북 선택·실행 절차 검증
│       ├── executionApproval.ts   # 승인 스냅샷 직렬화·digest·멱등 예약 비교
│       ├── sessionIndex.ts        # 세션 목록 인덱스 키 + 페이지 커서 인코딩
│       └── fencing.ts             # 취소·삭제의 claim/lease 조건부 쓰기
├── nuxt.config.ts                 # Nuxt 설정 (포트 3100, runtimeConfig)
├── package.json
└── tsconfig.json
```

## Configuration

`nuxt.config.ts`의 `runtimeConfig`으로 관리한다. 환경변수로 오버라이드 가능.

| 변수                         | 기본값                   | 설명                                                                                                                |
| ---------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| `AWS_REGION`                 | `us-east-1`              | AWS 리전                                                                                                            |
| `DYNAMODB_TABLE_NAME`        | `RcaAgentDevRcaSession`  | DynamoDB 테이블명                                                                                                   |
| `S3_REPORT_BUCKET`           | `rca-agent-dev-evidence` | S3 보고서·증거 버킷                                                                                                 |
| `EXECUTION_QUEUE_URL`        | (없음)                   | 실행 요청 큐 URL. 없으면 승인 발행이 503으로 실패한다 — 잘못 설정된 대시보드가 조용히 승인한 것처럼 보이면 안 된다. |
| `S3_VECTOR_BUCKET_NAME`      | (없음)                   | 플레이북 검색 벡터 버킷                                                                                             |
| `S3_VECTOR_REGION`           | `us-east-1`              | 벡터 버킷 리전                                                                                                      |
| `S3_VECTOR_PLAYBOOK_INDEX`   | `playbook`               | 플레이북 인덱스                                                                                                     |
| `BEDROCK_EMBEDDING_MODEL_ID` | `cohere.embed-v4:0`      | 지식 반영 후 검색 게시에 사용하는 임베딩 모델                                                                       |

## Agent Guidelines

### Safe to Modify

- 페이지 (`app/pages/`)
- 컴포넌트 (`app/components/`)
- 스타일 (`app/assets/css/`)
- API 라우트 (`server/api/`)

### Approach with Caution

- `nuxt.config.ts` — 프레임워크 설정
- `server/utils/aws.ts` — AWS 클라이언트 싱글톤

## Design System

**시각 디자인 토큰의 단일 출처는 [DESIGN.md](./DESIGN.md)다.** UI 작업 전에 그 문서를
먼저 읽는다.

방향은 **인시던트 운영 콘솔(incident operations cockpit)** 이다.

- 기본은 어두운 운영 화면이며 동일한 의미 체계를 가진 밝은 테마를 제공한다
- UI와 리포트는 산세리프, ID·시각·상태·명령은 고정폭 글꼴을 사용한다
- 분석 중·승인 대기·해결·실패·미해결은 semantic color와 보이는 라벨을 함께 사용한다
- 홈 화면은 metric cards → filter controls → incident queue 순서로 읽힌다
- incident row는 상태·알람·요약·엔진·시각·지속시간·리포트 진입을 hover 없이 보여준다
- 런북 실행 승인은 운영 대상에 명령을 실행하므로 별도 강조한다. 지식 제안의 반영·기각도 쓰기 동작이며, 실행 승인과 구분한 제어·처리 상태를 보여준다.
- 보고서는 요약 → 단일 상세 모달의 두 단계로 읽는다. 모달에서 선택한 원인 사슬·타임라인·복구 절차·증거·실행 이력의 내용은 경계가 분명한 panel로 나누며, 선택하지 않은 분석 내용은 숨긴다.
- 지속시간 막대와 원인 사슬의 선형 구조는 데이터 표현으로 유지하되 장식적 타임라인은 사용하지 않는다

### Common Mistakes to Avoid

- 서버 사이드 API에서 AWS SDK 호출 시 클라이언트 사이드로 노출하지 않도록 주의 (`server/` 디렉토리 안에서만 AWS SDK 사용)
- **세션 목록을 `Scan`으로 되돌리지 말 것.** 이 테이블은 세션 1건당 트레이스 항목이 약 7배이므로
  순회 비용이 세션 수가 아니라 트레이스 총량에 비례하고, `Scan`은 정렬을 보장하지 않아 "최신 N건"을
  저장소가 잘라줄 수 없다. 목록은 `session-by-engine-index`를 읽는다
- 세션 목록 인덱스 키(`list_engine`/`list_created_at`)를 이미 있는 `engine`·`created_at`으로
  대체하지 말 것. 가설·실행 항목도 그 두 속성을 갖고 있어 인덱스에 함께 들어오고(실측 91→641건),
  한 페이지가 대부분 가설로 채워져 페이징이 성립하지 않는다. 세션만 쓰는 키여야 sparse index가 된다
- 새 세션 쓰기 경로를 추가하면 인덱스 키도 함께 써야 한다. 빠뜨리면 그 세션은 목록에서 사라진다
- 전체 집계를 목록 페이지 응답에 합치지 말 것. 페이지 기준으로 세면 "승인 대기 10건"이 "3건"으로
  줄어 사람이 남은 일의 양을 틀리게 안다
- DynamoDB 예약어(`state` 등)를 표현식에 직접 쓰지 말고, 별칭은 쓴 곳마다 `ExpressionAttributeNames`에
  선언할 것. 미선언 별칭은 타입체크를 통과하고 요청 시점에 400으로 실패한다
- **`COMPLETED`을 "할 일이 없다"로 읽지 말 것.** 분석이 끝났다는 뜻일 뿐이고, 승인 대기인지는
  완료 · 확정 원인 · 유효한 실행 절차 · 진행 중 실행 없음으로 판단된다. 이 판정은
  `server/utils/playbook.ts`의 정확한 플레이북 선택·런북 명령 검증을 사용하고 승인 엔드포인트가 강제하는 조건과 같아야 한다 —
  서버가 409로 거부할 승인을 목록이 권하면 승인 게이트가 무의미해진다
- 중단 지점을 세션 레코드만으로 말하지 말 것. 종료 상태가 그 단계를 덮어쓰므로 `state`만으로는
  보고서 생성 중 죽은 세션과 첫 메트릭 호출에서 죽은 세션이 똑같이 보인다. 도달 단계는
  스팬에서만 나오고, `server/utils/progress.ts`가 그것을 계산한다
- 5 Whys 파싱을 신뢰하지 말고 방어할 것. 두 엔진 모두 산문으로만 쓰고 구분자가 다르다
  (`→` vs `—`). `app/utils/causalChain.ts`는 실패 시 빈 배열을 주고, 리포트 페이지는
  사슬이 없으면 본문 전문으로 대체한다 — 절반만 파싱된 사슬은 없는 것보다 나쁘다
- 시각을 ambient 타임존으로 렌더하지 말 것. SSR과 hydration이 어긋나 로드 직후 모든
  타임스탬프가 조용히 바뀐다. 표시 존을 명시한다
- `~`를 취소선으로 파싱되게 두지 말 것. 한국어 리포트에서 `~`는 범위 구분자(`05:41~05:42`)이고,
  GFM은 한 줄의 두 번째 `~`를 취소선 종료로 읽어 **리포트가 자기 결론을 지운 것처럼** 보인다.
  `app/utils/markdown.ts`가 `del` 토크나이저를 끈 이유다
- 상태 라벨에 저장 계층 어휘를 쓰지 말 것. `OUTDATED`는 TTL 만료가 아니라 알람이 너무
  오래되어 분석 진입 자체를 건너뛴 판정이다 (Strands 30분 / Headless Codex 60분)
- DynamoDB Scan 시 `begins_with(PK, 'RCA#')` 필터로 멱등성 키(`IDEMP#`) 레코드를 제외해야 함
- 실행 항목(`EXEC#`)을 세션 상태에 병합하지 말 것 — 실행은 분석과 별도 생명주기이고, 실행 실패가 완료된 분석을 실패로 보이게 하면 안 된다
- 실행 상태를 뱃지로 보일 때 `UNRESOLVED`·`FAILED`를 성공과 같은 강도로 표시하지 말 것 — 미해결 장애가 완료로 읽힌다
- 승인 발행 전 검증(분석 완료, 확정 원인, S3 리포트 존재, 완전하고 중복 ID가 없는 실행 절차)을 건너뛰지 말 것. 승인 시점 플레이북의 결정적 JSON 바이트와 SHA-256을 S3에 고정하고, 큐 발행 전 `PENDING_APPROVAL` 실행과 `EXEC_ACTIVE`를 한 트랜잭션으로 예약한다. 진행 중 실행의 권위는 실행 이력 조회가 아니라 `EXEC_ACTIVE` 조건부 쓰기다
- 승인 요청의 `approvalId`는 클라이언트가 한 승인 시도 동안 유지하는 UUID이며 `execution_id`와 동일하다. `requested_by`는 서버가 항상 `dashboard`로 기록하고 클라이언트 값을 신뢰하지 않는다
- 모델·S3에서 온 Markdown을 `marked`로 직접 렌더하지 말 것 — raw HTML이 그대로 보존되어 인증 없는 cancel/delete API를 호출할 수 있다. `app/utils/markdown.ts`를 사용한다
- 취소를 상태 변경만으로 구현하지 말 것. 실행 중 워커는 claim token을 계속 들고 있으므로 회전 없이는 취소 이후에도 산출물을 기록한다
- 활성 세션·실행을 삭제하지 말 것. 삭제는 fencing 기준을 제거하므로, 진행 중 실행과 SQS 재전달이 동시에 도는 상태를 만든다

- 명령 없는 과거 실행 계획은 원문을 읽을 수 있지만 새 실행 승인은 제공하지 않는다. 자연어에서 명령을 추정하거나 과거 실행 증거를 현재 명령으로 복사하지 않는다.
- 같은 플레이북의 현재 회고 개정본이 깨졌거나 명시적으로 미게시·만료되었으면 과거 런북으로 돌아가지 않는다. 현재 런북 조회와 새 승인은 실패하지만, 완료 원본에 연결된 생성 참고 자료 archive는 독립적으로 읽는다.
- 런북의 `commands`는 순서 있는 고정 AWS CLI 명령이다. `metric_wait`는 승인된 대상과 규칙으로 조치 후 두 60초 구간을 관측하는 별도 연산으로 표시한다.
- 라이브러리 조회는 `server/utils/playbookLibrary.ts`, wire 타입은 `shared/types/playbook-library.ts`가 소유한다. `GET /api/playbook-library`, `GET /api/playbook-library/:id`, `GET /api/playbook-proposals/:rcaId?engine=...`는 쓰기를 수행하지 않는다.
- 제안 처리는 `POST /api/playbook-proposals/:rcaId/:proposalId?engine=...`의 `{action:'apply'|'reject'}`로 수행한다. RCA·엔진 권위는 요청과 완료 원본이며 모델이 제안한 현재 사고 식별자를 사용하지 않는다.
- 비교 원문의 보조 필드(`comparison`, `library_revision`, `source_engine`, `source_rca_id`, `stage`, `summary`, `output_summary`)는 baseline 도메인 비교에서 제외한다. 지식 필드 이외 변경과 기존 목록 항목을 누락한 after-image는 거부한다. 반영 시 after-image를 보정하지 않으며 표시한 diff와 같은 값을 저장한다.
- 현재 본문은 `PK=PLAYBOOK_LIBRARY, SK=playbook_id`, 불변 본문은 `PK=PLAYBOOK#<id>, SK=revision`이며 원본 60일을 따른다. 본문 없는 목록·게시 상태는 `PK=PLAYBOOK_LIBRARY_STATE, SK=playbook_id`로 90일 보존한다. STATE가 남아 있는데 본문이 사라졌으면 레거시 벡터로 되돌아가지 않는다.
- 완료 플레이북의 `comparison`은 상태·선택 ID·`original_sk`·`original_expires_at`·간단한 제안 상태만 보유한다. 전체 원본은 같은 RCA의 `<engine>#PLAYBOOK_COMPARISON#<id>` 행 `comparison_json`에 60일 보존한다. GET은 정확한 archive와 만료를 확인하고, 만료 후에는 `summary`와 조회 불가 사유만 반환한다. 제안 처리 결과는 `<engine>#PLAYBOOK_PROPOSAL#<id>`에서 90일 보존한다.
- `original_created_at`은 고정 원본 생성 시각이고 STATE의 만료는 최초 반영 시각을 기준으로 고정한다. 게시 재시도는 TTL을 연장하지 않는다. 벡터 키는 `<playbook_id>@<revision>`이며 게시 완료는 현재 HEAD·STATE·disposition의 조건부 전이로 기록한다.
- 사용자 반영 HEAD와 STATE는 요청 사고 `proposal_rca_id`와 원본 사고 `source_rca_id`를 보존한다. 삭제 claim은 HEAD와 STATE의 게시 대기·소유 RCA 검사를 세션 갱신과 같은 트랜잭션에서 수행한다. 본문이 사라져도 PENDING STATE의 소유권 검사를 생략하지 않는다. 반영·게시의 원본 조건은 `deleting_at` 부재를 요구한다.
- 새 동작 테스트는 `tests/harness/dashboard-playbook-library.test.mjs`와 `dashboard-drilldown.test.mjs`에 있다. 테스트용 API 우회나 운영 요청의 임의 AWS endpoint 설정을 추가하지 않는다.
