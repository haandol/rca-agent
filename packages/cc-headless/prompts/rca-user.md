아래 CloudWatch 알람이 발생했다. RCA 파이프라인을 실행하여 분석하라.

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

위 알람을 분석하고 구조화된 한글 RCA 보고서를 생성하라. 반드시 `save_artifact("report.md", ...)` 로 최종 보고서를 저장하라.
