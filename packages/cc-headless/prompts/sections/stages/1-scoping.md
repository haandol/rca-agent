## 1단계: 초기 스코핑 (직접 수행)

1. AWS Knowledge MCP로 해당 서비스의 장애 패턴, 서비스 제한, 트러블슈팅 가이드를 검색한다 (30초).
2. CloudWatch MCP로 알람 메트릭과 관련 메트릭 1-2개를 최근 30분 + 24시간 전 동일 구간과 비교한다.
3. **영향범위** 판단: `single` (단일 리소스), `service` (서비스 전체), `regional` (리전 전체).
4. **심각도** 판단: `low`, `medium`, `high`, `critical`.
5. 로그 검색이나 트레이스 분석은 이 단계에서 하지 않는다.

서비스별 메트릭 패턴:
- **ECS**: CPUUtilization, MemoryUtilization, RunningTaskCount, DesiredTaskCount
- **RDS**: CPUUtilization, FreeableMemory, DatabaseConnections, ReadLatency, WriteLatency
- **Lambda**: Duration, Errors, Throttles, ConcurrentExecutions

**완료 후 반드시 `save_artifact("scoping.json", ...)` 으로 저장한다.**
