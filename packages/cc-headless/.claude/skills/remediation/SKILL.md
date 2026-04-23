---
name: remediation
description: 자동 복구 및 검증 — Healthcare Service 장애 리셋 API 매핑, ECS 강제 배포 대체, 복구 후 메트릭 검증 절차. 자동 복구 단계와 복구 검증 단계에서 근본원인에 맞는 복구 조치를 결정하고 실행할 때 반드시 이 스킬을 참조한다. 복구, 리셋, 롤백, 재시작, 검증이 언급될 때 사용한다.
---

# 자동 복구 및 검증

## 복구 실행 조건

근본원인이 **확정(신뢰도 ≥ 0.8)**된 경우에만 자동 복구를 시도한다. 미확정 상태에서는 보고서에 권장 조치만 기록하고 실행하지 않는다.

## Healthcare Service 장애 리셋 API

`http://<HEALTHCARE_SERVICE_HOST>:8000` 엔드포인트:

| 근본원인 키워드 | 엔드포인트 | 설명 |
|--------------|-----------|------|
| connection leak, pool exhaustion, too many connections, DatabaseConnections 급증 | `POST /fault/db-leak/reset` | DB 커넥션 누수 리셋 |
| high CPU, CPU spike, CPU utilization, CPUUtilization 급등 | `POST /fault/high-cpu/reset` | CPU 스트레스 주입 중단 |
| memory pressure, OOM, high memory, FreeableMemory 급감 | `POST /fault/high-memory/reset` | 메모리 과부하 주입 중단 |
| slow query, read latency, query timeout, ReadLatency 급증 | `POST /fault/slow-query/reset` | 느린 쿼리 주입 중단 |

### 근본원인→엔드포인트 매핑 방법

1. 근본원인 텍스트에서 위 키워드를 검색한다
2. 여러 키워드가 매칭되면 가장 구체적인 것을 선택한다
3. 매칭 없으면 ECS 강제 배포로 대체한다

## ECS 강제 배포 (대체 수단)

Healthcare Service 리셋 API로 해결할 수 없는 경우:

1. 대상 서비스의 ECS 클러스터와 서비스 이름을 확인한다
2. `UpdateService`에 `forceNewDeployment: true`를 설정하여 롤링 재시작을 트리거한다
3. 배포 완료까지 최대 5분 소요될 수 있으므로, 검증은 즉시 수행하지 않고 30초 후 시작한다

### 적용 시나리오

- 코드 배포 관련 장애 (새 버전의 비효율 코드)
- 매칭되는 리셋 API 없는 기타 장애
- 리셋 API 호출 실패 시 fallback

## 복구 후 검증 절차

복구 조치 실행 후 **30초 대기** 한 뒤:

### 1단계: 알람 메트릭 재조회

원래 알람을 트리거한 메트릭을 CloudWatch MCP로 재조회한다:
- 최근 5분간 데이터를 Period=60으로 조회
- 알람 임계치와 비교

### 2단계: 정상화 판정

| 조건 | 판정 |
|------|------|
| 메트릭 값 < 알람 임계치 (최근 3개 데이터포인트) | **정상화** |
| 메트릭 값이 감소 추세이나 아직 임계치 이상 | **진행 중** — 추가 대기 권장 |
| 메트릭 값 변화 없거나 증가 | **실패** — 수동 개입 필요 |

### 3단계: 보고서에 기록

```markdown
## Remediation
- **조치**: [실행한 API/ECS 배포]
- **결과**: [성공/실패]
- **시각**: [UTC 타임스탬프]

## Verification
- **검증 시점**: 복구 후 [N]초
- **메트릭 상태**: [정상화/진행 중/실패]
- **현재 값**: [메트릭 값] (임계치: [값])
- **잔여 이슈**: [있으면 기록, 없으면 "없음"]
```
