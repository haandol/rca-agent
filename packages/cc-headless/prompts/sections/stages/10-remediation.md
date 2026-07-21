## 10단계: 복구 권고 작성 (실행 금지)

CC Headless는 분석 전용이므로 HTTP POST, ECS 변경, 배포, 재시작 또는 롤백을
직접 수행하지 않는다. 별도 Remediation Agent 또는 승인된 오퍼레이터가 검토할
복구 권고만 작성한다.

1. 근본원인과 신뢰도를 바탕으로 **제안할 액션**을 선택한다.
   - 커넥션 누수 / 풀 소진 → 후보 `POST /fault/db-leak/reset`
   - 높은 CPU / CPU 급등 → 후보 `POST /fault/high-cpu/reset`
   - 메모리 부족 / OOM → 후보 `POST /fault/high-memory/reset`
   - 느린 쿼리 / 읽기 지연 → 후보 `POST /fault/slow-query/reset`
   - 적합한 리셋 API가 없음 → 후보 `UpdateService(forceNewDeployment=true)`
2. 후보별 **사전조건**을 정량적으로 명시한다. 대상 리소스, 현재 상태, 신뢰도,
   가용성·백업 조건이 확인되지 않으면 실행 금지를 표시한다.
3. **승인 필요** 여부와 승인 주체를 기록한다. 불명확하면 승인 필요로 판정한다.
4. **롤백 조건**을 오류율, 알람 메트릭, 배포 안정화 상태 등 정량 기준으로 기록한다.
5. 보고서 `## 복구 권고`와 플레이북 `temporary_mitigation`에 위 내용을 기록하고
   `실행 상태: CC Headless 미실행`을 명시한다.
6. 수정한 `report.md`와 `playbook.json`을 `save_artifact`로 다시 저장한다.
