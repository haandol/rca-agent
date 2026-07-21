---
name: reporting
description: RCA와 실제 remediation 결과를 분리해 최종 report.md와 playbook.json을 작성하는 가이드
---

# RCA Report와 Playbook

## 입력

- 원본 알람 상세
- RCA 전문 에이전트의 최종 결과와 증거
- Remediation 상태: `NOT_ATTEMPTED`, `SUCCEEDED`, `FAILED`, `BLOCKED`

입력에 없는 실행·정상화 사실은 만들지 않는다.

## report.md

다음 섹션을 포함한다.

- `## 인시던트 요약`
- `## 영향`
- `## 증거 시간 범위`
- `## 근본 원인`과 확정 여부·신뢰도
- `## 5 Whys` (증거가 끊기면 추가 조사 필요로 종료)
- `## 뒷받침 증거`
- `## 가설 분석 경로`
- `## 복구 결과`
- `## 검증 상태`
- `## Action Items`

복구 결과는 실제 상태를 그대로 기록한다.

- `NOT_ATTEMPTED`: 미확정으로 자동 복구 미실행
- `SUCCEEDED`: 실행 endpoint와 성공 응답
- `FAILED`: 호출 실패 사유와 수동 조치 필요
- `BLOCKED`: 안전 게이트 차단 사유와 추가 조사 필요

복구 성공만으로 서비스 정상화를 주장하지 않는다. 별도 관측 증거가 없으면 검증
상태는 `관측 대기`로 기록한다.

`## 증거 시간 범위`에는 다음 두 라벨과 ISO-8601 시작·종료 시각을 반드시 쓴다.

- `Current alarm window`: 현재 알람의 상태 변경 시각을 기준으로 조사한 구간
- `Historical comparison window`: 정상 baseline 또는 과거 비교를 위해 조회한 구간

모든 증거 항목에 어느 window에서 관측했는지 표시한다. 이번 실행의 current alarm
window보다 이전에 생성된 수동 장애 주입·수동 테스트 로그는 historical context일
뿐이며 현재 장애의 발생·원인·지속 증거로 서술하지 않는다. 시각이 없거나 window를
판별할 수 없는 로그는 현재 장애 증거에서 제외한다.

## playbook.json

기존 장애 유형, 증상 패턴, 정량 검증 절차, 임시·영구 조치, 에스컬레이션,
예방 조치 필드를 유지하고 다음 객체를 추가한다.

```json
{
  "remediation_result": {
    "status": "NOT_ATTEMPTED | SUCCEEDED | FAILED | BLOCKED",
    "fault_type": "db-leak",
    "endpoint_path": "/fault/db-leak/reset",
    "reason": "실행 또는 차단 결과",
    "validation_artifact": "validation-2.json",
    "verification": {
      "status": "NORMALIZED | FAILED | PENDING",
      "reason": "서버 측 bounded CloudWatch 검증 결과"
    }
  }
}
```

`remediation.json`은 서버 소유 원본이다. status, fault type, endpoint path,
validation artifact, verification을 `report.md`와 `playbook.json`에 그대로 반영한다.
Report 전문 에이전트는 `report.md`와 `playbook.json`을 모두 저장한다.
