# ADR 0007: RCA 보고서 생성 — LLM 기반 자동 보고서 작성

Date: 2026-04-21
Updated: 2026-05-25 (Toyota / AWS COE 5 Whys 사고 프레임 도입)

## Status

Accepted (Updated — 5 Whys 섹션 추가)

## Context

근본 원인이 확정(또는 미확정)된 후 SRE 팀이 활용할 수 있는 종합 RCA 보고서를 자동으로 생성해야 한다. 수동 보고서 작성은 수시간이 소요되며, 에이전트가 이미 수집한 증거와 추론 경로를 구조화하면 작성 시간을 크게 단축할 수 있다.

## Decision

**LLM 기반 구조화 보고서 자동 생성** 전략을 채택한다.

### 보고서 구조

- 장애 요약 (알람 정보, 발생 시각, 영향 범위)
- 심각도 (서비스 영향 범위와 지속 시간 기반: critical/high/medium/low)
- 영향 평가 (영향받은 서비스, 사용자 범위, 지속 시간, 에러율/지연 등 정량적 영향)
- 탐지 방법 (어떤 알람/메트릭/모니터링이 인시던트를 감지했는지)
- 근본 원인 (확정된 가설, 신뢰도)
- **5 Whys** (증상 → 시스템·프로세스 결함까지의 인과 사슬 — Toyota / AWS COE 가드레일 적용)
- 가설 도출 경로 (트리에서 근본 원인까지의 경로, 기각된 가설 목록)
- 증거 목록 (메트릭 스냅샷, 로그 스니펫, 배포 이력, 코드 diff 참조)
- 임시 조치 방안 (예: 서비스 롤백, 리소스 증설)
- 영구 조치 방안 (예: 코드 수정, 설정 변경)
- 조치 항목 (재발 방지, 영향 축소, 프로세스 개선으로 분류된 후속 작업)
- 교훈 (탐지/대응에서 잘된 점, 개선할 점, 운이 좋았던 점)
- 타임라인 (알람 발생 → 스코핑 → 가설 생성 → 검증 → 확정까지 시간 흐름)

### 핵심 결정사항

1. **Strands SDK structured output**: `ReportOutput` Pydantic 모델(`incident_summary`, `severity`, `impact_summary`, `detection_method`, `root_cause`, `temporary_mitigation`, `permanent_remediation`, `action_items`, `lessons_learned`, `timeline`, `five_whys`)을 `structured_output_model`로 지정한다. LLM 출력을 `RcaReport` 모델로 변환하여 `rca_id`, `hypothesis_path`, `evidence_list`, `rejected_hypotheses` 등 오케스트레이션 레이어에서 수집한 메타데이터를 추가한다.

2. **Markdown 저장**: `save_report_to_s3()`가 `_render_markdown()`으로 구조화된 Markdown을 생성하여 S3에 저장한다. 키 형식은 `reports/{rca_id}.md`이다. S3 버킷 미설정 시 업로드를 건너뛴다.

3. **근본 원인 미확정 처리**: `root_cause_confirmed=False`이면 Markdown에 "Unconfirmed (most likely candidate)" 라벨을 표시한다.

4. **타임아웃 및 fallback**: `ThreadPoolExecutor` 120초 타임아웃을 적용하며, 실패 시 알람 요약과 best hypothesis 정보만으로 최소 보고서(`RcaReport`)를 생성하여 파이프라인이 중단되지 않도록 한다.

5. **모델 티어**: **Planning 티어**(Sonnet 4.6 + adaptive thinking)를 사용한다. 구조화된 보고서 작성에 깊은 추론이 필요하다. [ADR agent/0010](0010-model-tier-architecture.md) 참조.

6. **5 Whys 사고 프레임 (Toyota / AWS COE)**: 보고서의 5 Whys 섹션은 단순 양식이 아니라 증상 → 시스템·프로세스·설계 결함까지의 인과 사슬 추적이다. LLM 프롬프트에 다음 가드레일을 강제한다.
   - **"휴먼 에러" 종착 금지**: "운영자 실수" 같은 답에서 멈추지 않고 "왜 그게 가능했는가? (검증 부재? 권한 과다? 런북 결함?)" 까지 풀어 시스템·프로세스 결함으로 표현.
   - **단일 원인 가정 회피**: 다요인이 함께 작용한 경우 주축 사슬은 가장 부하가 큰 요인으로 잡되, 각 단계의 답에 공동 요인(co-factors)을 한 줄로 명시.
   - **사실 기반 절단**: 각 "왜?"의 답은 수집된 증거 또는 타임라인 이벤트와 연결되어야 하며, 추측이면 사슬을 거기서 자르고 "증거 부족" 으로 명시.
   - **Blameless 톤**: 사람·팀을 비난하지 않고 시스템 관점으로만 기술.

   가드레일은 가설 생성 단계에도 동일하게 적용된다([ADR agent/0002](0002-hypothesis-tree-lifecycle.md)). cc-headless 엔진은 시스템 프롬프트의 공통 원칙·보고서 stage 프롬프트로 동일한 가드레일을 주입한다([ADR agent/0011](0011-cc-headless-prompt-driven-rca.md)).

## Consequences

### Positive

- 수동 보고서 작성 시간(수시간)을 자동화로 크게 단축
- 에이전트의 추론 경로와 증거가 체계적으로 문서화
- 표준화된 구조로 보고서 품질 일관성 확보

### Negative

- LLM 생성 보고서의 문체가 조직 표준과 다를 수 있음
- 증거 요약 과정에서 핵심 정보가 누락될 수 있음

### Risks

- S3에 저장된 증거 데이터 조회 실패 시 메타데이터만으로 보고서를 작성하게 되어 증거 세부 내용이 부족할 수 있다.

## Related

- [ADR agent/0002: 가설 트리 라이프사이클](0002-hypothesis-tree-lifecycle.md) — 5 Whys 가드레일이 가설 생성·분기에도 적용됨
- [ADR agent/0006: 중단 조건 판단](0006-termination-conditions.md) — 중단 후 보고서 생성으로 전이
- [ADR agent/0011: CC headless 파이프라인](0011-cc-headless-prompt-driven-rca.md) — cc-headless 엔진의 보고서 프롬프트에도 동일 가드레일 주입
