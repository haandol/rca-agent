## 오케스트레이션 순서

| 순서 | 전문 에이전트 | 조건 | 필수 결과 |
|------|-------------|------|----------|
| 1 | `rca-specialist` | 항상 | RCA 산출물과 확정 여부 |
| 2 | `remediation-specialist` | 최신 validation에 confirmed 존재 | 실제 복구 결과 |
| 3 | `report-specialist` | 항상 | `report.md`, `playbook.json` |

RCA가 미확정이면 Remediation 결과를 `NOT_ATTEMPTED`로 만들어 Report에 전달한다.
Remediation이 `BLOCKED` 또는 `FAILED`를 반환해도 Report를 반드시 호출한다.
