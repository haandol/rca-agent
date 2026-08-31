#### scoping.json

```json
{
  "stage": "SCOPING",
  "alarm_name": "알람 이름",
  "impact_scope": "single | service | regional",
  "severity": "low | medium | high | critical",
  "metric_observations": [
    {
      "metric_name": "DatabaseConnections",
      "datapoints": [2, 12, 20, 27, 30],
      "trend": "rising",
      "shape_note": "",
      "window_start": "ISO-8601",
      "window_end": "ISO-8601",
      "unit": "Count",
      "baseline": 2
    }
  ],
  "concurrent_alarms": [
    {"alarm_name": "동시 발생 알람", "state": "ALARM"}
  ],
  "summary": "스코핑 결과 요약 (한글)",
  "output_summary": "영향범위: service, 심각도: high"
}
```
