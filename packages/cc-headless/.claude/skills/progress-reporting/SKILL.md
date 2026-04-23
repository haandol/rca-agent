---
name: progress-reporting
description: rca-progress MCP 사용 가이드 — 파이프라인 단계별 DDB 상태 업데이트, 가설 저장/갱신, 산출물 파일 관리, 취소 확인 방법. 파이프라인 진행 상황을 기록하거나 취소 여부를 확인할 때 이 스킬을 참조한다.
---

# rca-progress MCP 사용 가이드

## 세션 ID

rca-progress MCP는 `/tmp/rca-session-id` 파일에서 현재 세션 ID를 읽는다. 이 파일은 Python 래퍼(main.py)가 알람 수신 시 자동 생성하므로, 에이전트가 별도로 생성할 필요 없다. 산출물은 `/tmp/rca-{세션ID}/` 디렉토리에 저장된다.

## 사용 가능한 도구

### `report_progress(stage, summary)`

파이프라인 단계를 DDB 세션 상태에 반영하고 span을 기록한다.

**stage 값:**
- `SCOPING` — 초기 스코핑 완료 시
- `HYPOTHESIS_GENERATION` — 가설 생성 완료 시
- `HYPOTHESIS_PRIORITIZATION` — 우선순위 결정 완료 시
- `EVIDENCE_COLLECTION` — 증거 수집 완료 시
- `HYPOTHESIS_VALIDATION` — 가설 검증 완료 시
- `REPORT_GENERATION` — 보고서 생성 시작 시
- `REMEDIATION` — 자동 복구 시작 시
- `VERIFICATION` — 복구 검증 시작 시

**반환값:**
- `{"ok": true, "cancelled": false}` — 정상
- `{"ok": false, "cancelled": true}` — 세션이 CANCELLED 상태. **즉시 분석을 중단**하고 현재까지의 결과로 보고서를 생성하라.

### `save_hypotheses(hypotheses_json)`

가설 목록을 DDB에 배치 저장한다. JSON 배열 문자열을 전달한다.

### `update_hypothesis(hypothesis_id, status, confidence_score, reasoning, evidence_summary)`

개별 가설의 검증 결과를 DDB에 반영한다.

### `save_artifact(filename, content)`

마크다운 산출물을 `/tmp/rca-{세션ID}/` 아래에 저장한다. 세션 ID는 `/tmp/rca-session-id` 파일에서 자동으로 읽는다.

### `check_cancelled()`

현재 세션이 CANCELLED 상태인지 확인한다. 검증 루프 시작 전에 호출하여 불필요한 작업을 방지한다.

## 호출 타이밍

### 메인 에이전트가 직접 호출

| 시점 | 호출 |
|------|------|
| 스코핑 완료 후 | `report_progress("SCOPING", "알람 분석 완료: ...")` |
| 가설 생성 서브에이전트 완료 후 | `report_progress("HYPOTHESIS_GENERATION", "5개 가설 생성")` |
| 보고서 생성 시작 시 | `report_progress("REPORT_GENERATION", "보고서 생성 시작")` |
| 자동 복구 시작 시 | `report_progress("REMEDIATION", "...")` |
| 복구 검증 시작 시 | `report_progress("VERIFICATION", "...")` |
| 검증 루프 시작 전 | `check_cancelled()` |

### 검증 서브에이전트가 호출

| 시점 | 호출 |
|------|------|
| 우선순위 결정 후 | `report_progress("HYPOTHESIS_PRIORITIZATION", "...")` |
| 증거 수집 후 | `report_progress("EVIDENCE_COLLECTION", "...")` |
| 각 가설 검증 후 | `update_hypothesis(...)` |
| 검증 완료 후 | `report_progress("HYPOTHESIS_VALIDATION", "...")` |
| 산출물 저장 | `save_artifact("validation-N.md", "...")` |

## 취소 처리

`report_progress`가 `{"cancelled": true}`를 반환하면:
1. 현재 진행 중인 증거 수집/검증을 중단한다
2. 지금까지 수집된 증거와 가설로 보고서를 생성한다
3. 보고서에 "분석이 관리자에 의해 중단됨"을 명시한다
