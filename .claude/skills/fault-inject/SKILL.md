---
name: fault-inject
description: Healthcare 서비스에 장애를 주입하거나 리셋한다. "장애 인젝션", "fault inject", "인젝션", "inject", "db leak", "high cpu", "high memory", "slow query", "커넥션 누수", "CPU 장애", "메모리 장애", "쿼리 장애", "장애 리셋", "fault reset", "장애 테스트", "RCA 테스트" 등의 키워드에 트리거. 장애를 주입하고 CloudWatch 알람이 트리거되어 RCA 에이전트가 자동 분석을 시작하는 E2E 테스트에 사용한다.
---

# fault-inject

Healthcare 서비스(ECS Fargate)에 장애를 주입하거나 리셋하는 스킬.

## 사전 조건

Healthcare 서비스는 프라이빗 VPC 내부에서 실행되므로 직접 HTTP 호출이 불가하다. ECS Exec을 통해 컨테이너 내부에서 localhost로 호출한다.

## 장애 유형

| 유형 | 주입 엔드포인트 | 리셋 엔드포인트 | 트리거 알람 |
|------|---------------|---------------|-----------|
| DB 커넥션 누수 | `POST /fault/db-leak` | `POST /fault/db-leak/reset` | `RcaAgentDev-Healthcare-RdsHighConnections` (임계치: 30) |
| High CPU | `POST /fault/high-cpu` | `POST /fault/high-cpu/reset` | `RcaAgentDev-Healthcare-HighCPU` (임계치: 80%) |
| High Memory | `POST /fault/high-memory` | `POST /fault/high-memory/reset` | `RcaAgentDev-Healthcare-HighMemory` (임계치: 80%) |
| Slow Query | `POST /fault/slow-query` | `POST /fault/slow-query/reset` | 직접 알람 없음 (RDS ReadLatency로 간접) |

모든 엔드포인트는 `POST` 메서드, `Content-Type: application/json` body: `{"count": N}` (기본값 10).

## 실행 방법

### 1. Healthcare 태스크 ID 조회

```bash
aws ecs list-tasks --cluster RcaAgentDevHealthcare --service-name RcaAgentDevHealthcare --query 'taskArns[0]' --output text
```

태스크 ARN에서 마지막 `/` 뒤의 ID를 추출한다.

### 2. ECS Exec으로 장애 주입

컨테이너에 curl/wget이 없으므로 Python urllib을 사용한다.

```bash
aws ecs execute-command \
  --cluster RcaAgentDevHealthcare \
  --task <TASK_ID> \
  --container healthcare \
  --command "python -c 'import urllib.request; r=urllib.request.Request(\"http://localhost:8000/fault/<TYPE>\",data=b\"{\\\"count\\\": <N>}\",method=\"POST\",headers={\"Content-Type\":\"application/json\"}); print(urllib.request.urlopen(r).read().decode())'" \
  --interactive
```

`<TYPE>`: `db-leak`, `high-cpu`, `high-memory`, `slow-query` 중 하나.
`<N>`: 장애 강도. 유형별 권장값:

| 유형 | 알람 트리거 권장값 | 설명 |
|------|-----------------|------|
| db-leak | 50 | 커넥션 50개 누수 (임계치 30) |
| high-cpu | 10 | CPU 스트레스 스레드 10개 |
| high-memory | 10 | 메모리 할당 블록 10개 |
| slow-query | 10 | 느린 쿼리 10개 주입 |

### 3. 장애 리셋

```bash
aws ecs execute-command \
  --cluster RcaAgentDevHealthcare \
  --task <TASK_ID> \
  --container healthcare \
  --command "python -c 'import urllib.request; r=urllib.request.Request(\"http://localhost:8000/fault/<TYPE>/reset\",data=b\"{}\",method=\"POST\",headers={\"Content-Type\":\"application/json\"}); print(urllib.request.urlopen(r).read().decode())'" \
  --interactive
```

### 4. 알람 상태 확인

```bash
aws cloudwatch describe-alarms \
  --alarm-names "RcaAgentDev-Healthcare-RdsHighConnections" "RcaAgentDev-Healthcare-HighCPU" "RcaAgentDev-Healthcare-HighMemory" \
  --query 'MetricAlarms[*].{Name:AlarmName,State:StateValue}' \
  --output table
```

알람이 ALARM 상태로 전환되면 SNS→SQS 경로로 양쪽 에이전트(strands, cc-headless)에 알림이 전달된다. 알람 평가 주기(1분×2)에 따라 약 2-3분 후 ALARM으로 전환된다.

### 5. RCA 세션 확인

```bash
aws dynamodb scan \
  --table-name RcaAgentDevRcaSession \
  --filter-expression "contains(SK, :sess)" \
  --expression-attribute-values '{":sess": {"S": "SESSION"}}' \
  --query 'Items[*].{rca_id:rca_id.S,engine:engine.S,alarm:alarm_name.S,state:state.S,created:created_at.S}' \
  --output table
```

## 주의사항

- ECS Exec은 `--interactive` 플래그가 필요하고, 세션 완료 시 자동 종료된다.
- 장애 주입 후 알람 트리거까지 약 2-3분 소요.
- 테스트 후 반드시 리셋하여 서비스를 정상 상태로 복원할 것.
