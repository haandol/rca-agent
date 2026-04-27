# ADR 0008: 플레이북 생성 — 유사 장애 대응 자동화 및 S3 Vectors 인덱싱

Date: 2026-04-21

## Status

Accepted

## Context

동일 유형의 장애가 재발할 때 과거 경험을 즉시 활용하려면, RCA 결과를 재사용 가능한 플레이북으로 자동 변환하고 벡터 검색이 가능하도록 인덱싱해야 한다. 이 플레이북은 F2(초기 스코핑)의 유사 플레이북 검색에 데이터를 공급한다.

## Decision

**LLM 기반 플레이북 자동 생성 + S3 Vectors 임베딩 인덱싱** 전략을 채택한다.

### 플레이북 구조

- 장애 유형 분류 (예: DB 커넥션 누수, CPU 폭증, 메모리 릭 등)
- 증상 패턴 (어떤 알람/메트릭이 이 유형의 장애를 시사하는지)
- 확인 절차 (단계별 검증 방법 — 이번 RCA에서 검증한 경로 기반)
- 임시 조치 절차 (예: 롤백, 리소스 증설)
- 영구 조치 가이드 (코드 수정 방향, 설정 변경)
- 예방 조치 (모니터링 추가, 알람 임계치 조정)

### 핵심 결정사항

1. **검색 우선(Search-First) 전략**: 플레이북 생성 전에 S3 Vectors에서 유사 플레이북을 검색한다. 유사도 0.86 이상(`PLAYBOOK_UPDATE_THRESHOLD`)인 기존 플레이북이 있으면 LLM이 새 RCA 결과와 비교하여 업데이트할 내용이 있는지 판단한다. 업데이트가 필요하면 기존 플레이북 ID를 유지한 채 내용을 보강하고, 업데이트가 불필요하면 새 플레이북 생성도 건너뛴다. 일치하는 기존 플레이북이 없을 때만 새로 생성한다.

2. **벡터 임베딩**: Bedrock **Cohere Embed V4** (`cohere.embed-v4:0`, 1536차원)를 사용하며, 구조화된 템플릿으로 임베딩 텍스트를 생성한다:
   ```
   장애유형: {failure_type} | 증상: {symptom_pattern} | 메트릭: {metric_name}
   ```
   각 필드는 80자로 truncate하여 임베딩 품질을 유지한다. 저장 시 `input_type=search_document`, 검색 시 `input_type=search_query`를 사용한다. 스코핑 단계의 유사 플레이북 검색에서도 동일 템플릿(`장애유형: {alarm_name} | 증상: {state_reason} | 메트릭: {metric_name}`)을 적용하여 저장-검색 간 임베딩 공간 일관성을 보장한다.

3. **업데이트 판단 LLM**: `PlaybookUpdateOutput` Pydantic 모델(`needs_update`, 플레이북 전체 필드)을 `structured_output_model`로 지정하여 LLM이 기존 플레이북 대비 새 RCA에서 추가된 점이 있는지 판단한다. `needs_update=false`이면 기존 플레이북을 그대로 유지한다.

4. **S3 Vectors 인덱싱**: `save_playbook_to_s3_vectors()`가 Cohere Embed V4로 `float32` 벡터를 직접 생성하여 저장한다. 메타데이터는 S3 Vectors의 2048 bytes 제한에 맞춰 `failure_type`(80자), `symptom_pattern`(80자), `tags`(CSV 256자), `rca_id`만 포함한다. `verification_steps`, `temporary_mitigation` 등 상세 필드는 DynamoDB의 플레이북 레코드에서 조회한다.

5. **태깅**: LLM이 생성한 `tags` 필드를 플레이북에 포함하여 유형별 분류에 활용한다.

6. **타임아웃 및 fallback**: 각 LLM 호출에 `ThreadPoolExecutor` 120초 타임아웃을 적용하며, 실패 시 `failure_type="unknown"`, `symptom_pattern=incident_summary`로 최소 플레이북을 생성한다.

7. **비차단(non-blocking) 실행**: 플레이북 생성은 파이프라인을 중단시키지 않는다. 생성 실패 시 FAILED 스팬을 기록하고 파이프라인은 알림/세션 완료로 계속 진행한다. 이는 Strands(`start_span`/`end_span` + try/except)와 CC Headless(Python wrapper `_process_playbook()`) 모두에 적용된다.

8. **모델 티어**: **Planning 티어**(Sonnet 4.6 + adaptive thinking)를 사용한다. 장애 패턴 추출, 절차 작성, 기존 플레이북 업데이트 판단에 추론이 필요하다. [ADR agent/0010](0010-model-tier-architecture.md) 참조.

## Consequences

### Positive

- 검색 우선 전략으로 중복 플레이북 축적 방지 — 동일 유형 장애의 플레이북이 점진적으로 보강됨
- 과거 RCA 경험이 플레이북으로 체계적으로 축적되어 조직 지식 자산화
- S3 Vectors 인덱싱으로 유사 장애 발생 시 즉시 검색하여 스코핑 단계(F2)에서 활용 가능
- 재발 장애에 대한 MTTR 추가 단축

### Negative

- 플레이북 품질이 RCA 결과의 정확도에 의존 — 오판된 RCA에서 잘못된 플레이북 생성 가능
- S3 Vectors 임베딩 저장 실패 시 검색 불가(원본은 S3에 보존)
- 업데이트 판단을 위한 LLM 호출이 추가로 발생 (기존 플레이북당 1회)

### Risks

- 근본 원인 미확정 RCA에서 생성된 플레이북이 향후 유사 장애 시 오도할 수 있다. "추정 원인 기반" 플레이북으로 명시하고 검증 부족 항목을 표기하여 완화한다.

## Implementation Status

- **플레이북 생성/저장/인덱싱**: 구현 완료 (Strands F8 단계, CC Headless 9단계). Cohere Embed V4(1536차원) + `float32` 벡터 직접 저장 방식으로 E2E 검증 완료. 양쪽 엔진 모두 S3 Vectors에 벡터 저장 성공 확인.
- **유사 플레이북 검색**: 구현 완료 (Scoping 단계에서 S3 Vectors 검색). 구조화 임베딩 템플릿(`장애유형 | 증상 | 메트릭`)으로 저장-검색 간 일관성 확보.
- **플레이북 기반 자동 복구**: **미구현** — ADR agent/0012에 따라 별도 Remediation Agent가 SNS → SQS로 구독하여 플레이북의 복구 절차를 실행하도록 설계되었으나, Remediation Agent가 아직 배포되지 않음. `remediation.py`(복구 실행)와 `verification.py`(복구 검증) 모듈은 준비됨
- **대시보드 표시**: 구현 완료 (전용 플레이북 페이지 `/playbook/:id`, 세션 목록 및 트레이스 뷰에서 링크)

현재 플레이북은 생성 → Cohere Embed V4 임베딩 → S3 Vectors 인덱싱 → SNS 알림 포함까지 수행되며, 다음 알람 수신 시 Scoping 단계에서 유사 플레이북으로 검색되어 가설 생성에 참고된다. 그러나 플레이북의 복구 절차(temporary_mitigation, permanent_remediation)를 자동으로 실행하는 경로는 아직 연결되지 않았다.

## Related

- [ADR agent/0007: RCA 보고서 생성](0007-rca-report-generation.md) — 플레이북의 입력인 RCA 보고서를 생성하는 단계
- [ADR agent/0001: 초기 스코핑 전략](0001-initial-scoping-strategy.md) — 플레이북을 유사도 검색하여 활용하는 단계
- [ADR agent/0012: 자동 복구](0012-automated-remediation.md) — 플레이북 기반 복구를 별도 에이전트로 분리 (미구현)
- [ADR infra/0002: 증거 저장](../infra/0002-evidence-storage.md) — 플레이북도 S3 + S3 Vectors에 저장
