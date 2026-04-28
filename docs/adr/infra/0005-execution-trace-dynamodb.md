# ADR 0005: 파이프라인 실행 트레이스 — DynamoDB 기반 단계별 추적

Date: 2026-04-23
Updated: 2026-04-24

## Status

Accepted (Updated — CC Headless 산출물 파일 기반 트레이싱 추가)

## Context

RCA 에이전트의 파이프라인은 12단계(스코핑→가설 생성→우선순위→증거 수집→검증→분기→종료→보고서→플레이북→치유→검증→알림)로 구성되며, 검증 루프를 최대 3회 반복한다. 현재 DynamoDB에는 세션 수준 상태만 저장되어, 대시보드에서는 "현재 어떤 단계인지"와 최종 보고서만 확인 가능하다.

문제점:

1. 각 단계의 소요 시간, 입출력 요약, 오류 원인을 확인할 수 없다
2. 가설 트리 구조(부모-자식 관계, 분기 과정)가 저장되지 않아 분석 과정을 재현할 수 없다
3. 검증 루프 반복 횟수와 각 루프의 결과를 추적할 수 없다
4. ADR infra/0002에서 설계한 "DynamoDB 가설 트리 상태 관리"가 아직 구현되지 않았다

검토한 대안:

- **OpenTelemetry + X-Ray**: 비즈니스 도메인 데이터(가설 설명, 판단 근거)를 span attribute로 저장하기에 부적합. X-Ray 트레이스는 30일 보존 기본이며 커스텀 쿼리가 제한적
- **별도 DynamoDB 테이블**: 관리 포인트 증가. 기존 단일 테이블 패턴 활용이 더 효율적
- **S3 JSON 파일**: 쿼리 불편. 실시간 상태 업데이트에 부적합

## Decision

기존 DynamoDB 단일 테이블에 `SPAN#` 및 `HYPO#` SK 접두사로 트레이스 데이터를 저장한다. 분산 트레이스의 span 개념을 차용하여 파이프라인의 각 단계를 추적하고, 가설 트리 노드를 별도 아이템으로 영속화한다.

### 아이템 스키마

기존 `PK="RCA#{rca_id}", SK="SESSION"` 아이템과 동일 파티션에 두 가지 새 아이템 유형을 추가한다:

1. **Span 아이템** (`SK="{engine}#SPAN#{span_id}"`): 파이프라인 각 단계의 시작/종료, 소요 시간, 입출력 요약, 에러, 메타데이터를 기록한다. parent_span_id로 중첩 구조(검증 루프 → 하위 단계)를 표현한다. SK 접두사에 엔진명(`strands` 또는 `cc-headless`)을 포함하여 엔진별 트레이스를 분리한다.
2. **Hypothesis 아이템** (`SK="{engine}#HYPO#{hypothesis_id}"`): 가설 트리 노드의 설명, 카테고리, 상태, 신뢰도, 증거 요약, 판단 근거를 기록한다. parent_id와 depth로 트리 구조를 표현한다.

### 쓰기 전략

엔진별로 쓰기 방식이 다르다:

**Strands Agent**: `trace_store.py` 모듈이 Python 코드에서 직접 DDB에 스팬을 기록한다. 각 파이프라인 단계의 시작/종료 시점에 `start_span`/`end_span`을 호출한다.

**CC Headless**: CC CLI가 MCP 도구로 직접 스팬을 기록하는 방식은 CC CLI가 "분석에 불필요한 도구"로 판단하여 호출을 건너뛰는 문제가 있었다. 이를 해결하기 위해 **산출물 파일 기반 트레이싱**을 도입했다:

1. CC CLI가 `save_artifact` MCP 도구로 `/tmp/rca-{id}/`에 JSON 산출물을 저장한다
2. Python wrapper의 `artifact_watcher` 스레드가 이 디렉토리를 3초 간격으로 폴링한다
3. 새 파일이 감지되면 JSON을 파싱하여 DDB에 스팬/가설 아이템을 기록한다

산출물 파일과 스팬 타입의 매핑:

| 산출물 파일 | 스팬 타입 | 추가 동작 |
|------------|----------|----------|
| `scoping.json` | `SCOPING` | — |
| `hypotheses.json` | `HYPOTHESIS_GENERATION` | 가설 목록을 HYPO 아이템으로 일괄 저장 |
| `validation-{N}.json` | `VALIDATION_LOOP` | confirmed/rejected/closed/needs_investigation로 HYPO 상태 갱신 |
| `report.md` | `REPORT` | — |

공통 규칙:
- 쓰기 실패는 로깅 후 무시한다 (분석 파이프라인을 차단하지 않음)
- 가설 노드는 `BatchWriteItem`으로 일괄 저장한다 (25개 청크)
- 입출력 요약은 500자로 제한한다 (전체 데이터는 S3에 별도 보존)
- JSON 파싱 실패 시 스팬은 `FAILED` 상태로 기록되고 `error` 필드에 원인이 기록된다

### 읽기 전략

- 단일 `Query(PK="RCA#{rca_id}")`로 세션 + 스팬 + 가설 전체를 조회한다
- 대시보드 목록 페이지의 기존 `Scan(SK="SESSION")` 쿼리는 변경하지 않는다
- 새 GSI는 필요하지 않다

### 대시보드 연동

- `GET /api/traces/{rca_id}` 엔드포인트로 트레이스 데이터를 제공한다
- 스팬 타임라인(Gantt 형태)과 가설 트리(재귀 구조) 시각화 페이지를 추가한다

## Consequences

### Positive

- 파이프라인 실행 과정을 분산 트레이스처럼 시각화할 수 있다
- 가설 트리 구조와 판단 과정(증거 요약, 판단 근거, 신뢰도 변화)을 대시보드에서 확인할 수 있다
- 기존 테이블 재사용으로 인프라 변경이 없다 (CDK 스택 수정 불필요)
- TTL로 90일 후 자동 정리된다

### Negative

- Strands: 파이프라인 단계마다 DynamoDB 쓰기 2회 추가 (start + end)
- CC Headless: 산출물 파일당 DynamoDB 쓰기 1회 (파일 생성 시점에 완료 기록)
- 가설 10개 기준 BatchWriteItem 1회 + update 10회 추가
- 세션 목록 Scan 시 SPAN/HYPO 아이템도 읽히지만 FilterExpression으로 제외됨 (소규모 RCA에서 무시 가능)
- CC Headless의 3초 폴링 간격으로 산출물 감지에 최대 3초 지연이 발생한다

### Risks

- DynamoDB 쓰로틀링 시 트레이스 데이터 유실 가능. fire-and-forget 패턴으로 분석 파이프라인은 영향 없음
- 아이템 수 증가로 테이블 비용 소폭 증가. 온디맨드 과금 모드에서 무시할 수 있는 수준
- 세션 수가 크게 증가하면 Scan 성능 저하 가능. 향후 GSI 추가로 대응
- CC Headless에서 CC CLI가 `save_artifact`를 호출하지 않으면 해당 단계의 트레이스가 누락된다. 프롬프트에서 반복적으로 산출물 저장을 강조하여 완화한다
- CC CLI가 malformed JSON을 출력하면 해당 스팬이 FAILED로 기록된다. 향후 CC CLI에 JSON 재생성을 요청하는 복구 메커니즘을 검토할 수 있다

## Related

- [ADR infra/0002: 증거 저장](0002-evidence-storage.md) — DynamoDB 가설 트리 저장 설계의 구현
- [ADR agent/0018: 가설 트리 라이프사이클](../agent/0018-hypothesis-tree-lifecycle.md) — 가설 트리의 부모-자식 관계 구조와 검증 루프
