아래 CloudWatch 알람에 대한 RCA 오케스트레이션을 실행하라.

## 알람 상세

- **알람 이름**: {alarm_name}
- **상태 사유**: {state_reason}
- **상태 변경 시각**: {state_change_time}
- **리전**: {region}

## 트리거

- **메트릭**: {namespace}/{metric_name}
- **차원**: {dimensions}
- **통계**: {statistic}
- **주기**: {period}초
- **임계치**: {threshold} ({comparison_operator})

`spawn_agent`로 `rca-specialist`를 먼저 호출하고, 확정 여부와 무관하게
`report-specialist`를 호출하라. 메인은 직접 분석하거나 산출물을 저장하지 않는다.
이 실행은 분석만 수행하며 어떤 복구도 실행하지 않는다.

최종적으로 현재 실행에 다음 산출물이 있어야 한다.

1. RCA: `scoping.json`, 초기 `hypotheses.json`, 필요 시 `hypotheses-2.json`·`hypotheses-3.json`, `validation-1..3.json`
2. Report: `report.md`, `playbook.json`
