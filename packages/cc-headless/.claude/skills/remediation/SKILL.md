---
name: remediation
description: 복구 권고 및 검증 계획 — Healthcare Service 장애 리셋 API와 ECS 강제 배포를 실행하지 않고 후보로 제안하며, 사전조건·승인·롤백·검증 판정 기준을 작성한다. 복구, 리셋, 롤백, 재시작, 검증이 언급될 때 사용한다.
---

# 복구 권고 및 검증 계획

## 책임 경계

CC Headless는 분석 전용이다. HTTP POST, ECS `UpdateService`, 배포, 재시작,
롤백을 직접 수행하지 않으며 변경 도구를 요구하지 않는다. 근본원인의 확정 여부와
관계없이 실제 변경은 별도 Remediation Agent 또는 승인된 오퍼레이터의 책임이다.

근본원인이 **확정(신뢰도 ≥ 0.8)**되면 구체적인 복구 후보를 작성한다. 미확정이면
추가 조사와 승인 전 실행 금지를 명시한 조건부 후보만 작성한다.

## Healthcare Service 장애 리셋 API 후보

`http://<HEALTHCARE_SERVICE_HOST>:8000` 엔드포인트:

| 근본원인 키워드 | 제안할 액션 후보 | 기대 효과 |
|--------------|-------------------|----------|
| connection leak, pool exhaustion, too many connections, DatabaseConnections 급증 | `POST /fault/db-leak/reset` | DB 커넥션 누수 리셋 |
| high CPU, CPU spike, CPU utilization, CPUUtilization 급등 | `POST /fault/high-cpu/reset` | CPU 스트레스 주입 중단 |
| memory pressure, OOM, high memory, FreeableMemory 급감 | `POST /fault/high-memory/reset` | 메모리 과부하 주입 중단 |
| slow query, read latency, query timeout, ReadLatency 급증 | `POST /fault/slow-query/reset` | 느린 쿼리 주입 중단 |

### 후보 선택 방법

1. 근본원인 텍스트에서 위 키워드를 검색한다
2. 여러 키워드가 매칭되면 가장 구체적인 것을 선택한다
3. 매칭이 없으면 ECS 강제 배포를 대체 후보로 검토한다
4. 선택한 액션은 실행하지 않고 보고서와 플레이북에 권고로 기록한다

## ECS 강제 배포 후보

Healthcare Service 리셋 API로 해결할 수 없는 경우:

1. 대상 서비스의 ECS 클러스터, 서비스, 현재 태스크 정의를 사전조건으로 명시한다
2. `UpdateService(forceNewDeployment=true)`를 승인 후 수행할 후보로만 기록한다
3. 서비스 소유자 또는 온콜의 승인 필요 여부와 승인 주체를 명시한다
4. 새 태스크 비정상, 오류율 증가, 배포 안정화 실패 시의 롤백 조건을 명시한다

### 적용 시나리오

- 코드 배포 관련 장애 (새 버전의 비효율 코드)
- 매칭되는 리셋 API 없는 기타 장애
- 별도 실행 주체가 리셋 API를 시도했으나 실패한 경우의 fallback

## 권고에 반드시 포함할 항목

보고서의 `## 복구 권고`와 플레이북의 `temporary_mitigation`에 다음을 기록한다:

- **제안할 액션**: 별도 실행 주체가 수행할 구체적인 API 또는 ECS 후보
- **사전조건**: 근본원인 신뢰도, 대상 리소스, 현재 상태, 백업·가용성 조건
- **승인 필요**: 승인 필요 여부와 승인 주체. 불명확하면 항상 승인 필요
- **롤백 조건**: 오류율 증가, 메트릭 악화, 안정화 실패 등 정량 조건
- **실행 상태**: `CC Headless 미실행`

## 검증 계획

실제 복구 이후 별도 실행 주체가 수행할 계획만 작성하며, CC Headless는 대기하거나
사후 검증을 수행했다고 주장하지 않는다.

각 검증 항목에 다음을 포함한다:

- **검증 메트릭**: namespace, metric, dimensions, period, 관측 구간
- **기준값**: 장애 전 baseline과 알람 임계치
- **판정 기준**: 정상화/진행 중/실패를 구분하는 정량 Pass/Fail 조건
- **실패 대응**: 롤백 또는 에스컬레이션 조건과 담당 주체
- **관측 시작 조건**: 별도 실행 주체가 변경 성공을 확인한 뒤

```markdown
## 복구 권고
- **제안할 액션**: [API/ECS 후보]
- **사전조건**: [정량 조건]
- **승인 필요**: [예/아니오, 승인 주체]
- **롤백 조건**: [정량 조건]
- **실행 상태**: CC Headless 미실행

## 검증 계획
- **검증 메트릭**: [namespace/metric/dimensions]
- **관측 구간**: [period와 데이터포인트 수]
- **정상화 판정**: [Pass 기준]
- **실패 판정**: [Fail 기준과 롤백/에스컬레이션]
```
