## 10단계: 자동 복구 (직접 수행)

근본원인이 확정(신뢰도 ≥ 0.8)되면 자동 복구를 시도한다:

1. 근본원인 텍스트에서 장애 유형을 판별한다.
2. Healthcare Service 장애 리셋 API 엔드포인트를 호출한다:
   - 커넥션 누수 / 풀 소진 → `POST /fault/db-leak/reset`
   - 높은 CPU / CPU 급등 → `POST /fault/high-cpu/reset`
   - 메모리 부족 / OOM → `POST /fault/high-memory/reset`
   - 느린 쿼리 / 읽기 지연 → `POST /fault/slow-query/reset`
3. 매칭되는 엔드포인트 없으면 ECS 강제 새 배포를 시도한다.
4. 보고서에 `## 복구 조치` 섹션을 추가하고 수행한 조치와 결과를 기록한다.
