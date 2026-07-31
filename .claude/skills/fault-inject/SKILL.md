---
name: fault-inject
description: Healthcare 서비스에 장애를 주입하거나 리셋한다. "장애 인젝션", "fault inject", "인젝션", "inject", "db leak", "high cpu", "high memory", "slow query", "커넥션 누수", "CPU 장애", "메모리 장애", "쿼리 장애", "장애 리셋", "fault reset", "장애 테스트", "RCA 테스트" 등의 키워드에 트리거. 장애를 주입하고 CloudWatch 알람이 트리거되어 RCA 에이전트가 자동 분석을 시작하는 E2E 테스트에 사용한다.
---

# fault-inject

Healthcare 서비스(ECS Fargate)에 장애를 주입하거나 리셋하는 스킬.

## 사전 조건

Healthcare 서비스는 프라이빗 VPC 내부에서 실행되므로 직접 HTTP 호출이 불가하다. ECS Exec을 통해 컨테이너 내부에서 localhost로 호출한다.

## 주입 방식 선택

| 방식 | 용도 | CloudTrail 배포 이벤트 |
|------|------|----------------------|
| **배포 기반** (`scripts/inject_deployment_fault.py`) | 플래그십 데모, RCA 능력 시연 | 남는다 |
| **직접 API 호출** (`POST /fault/*`) | 빠른 수동 확인, 반복 테스트 | 남지 않는다 |

RCA가 배포 이력과 코드 변경으로 원인을 특정하는 과정을 보여주려면 **배포 기반**을
사용한다. 직접 호출은 흔적을 남기지 않으므로 변경 이력 조회에서 찾을 대상이 없다.

## 배포 기반 주입 (플래그십 데모)

로컬에서 실행하며 ECS Exec이 필요 없다.

```bash
python3 scripts/inject_deployment_fault.py status        # 현재 플래그 확인
python3 scripts/inject_deployment_fault.py db-leak       # 누수 리비전 배포
python3 scripts/inject_deployment_fault.py red-herring   # 무해한 배포 (레드헤링)
python3 scripts/inject_deployment_fault.py reset         # 모든 플래그 해제 리비전 배포
```

권장 데모 순서:

1. `red-herring` 실행 후 2~3분 대기 — 증상과 무관한 배포 이벤트를 먼저 심는다
2. `db-leak` 실행 — 이 배포가 실제 원인이다
3. 롤아웃과 점진적 누적을 거쳐 약 5~10분 후 `VitalIngestFailures` 알람 발생
4. RCA 세션 확인 (아래 5절)
5. 종료 후 `reset` 실행

리셋 API 호출만으로는 실행 중 프로세스의 상태만 해소된다. 태스크 정의 플래그가 남아
있으면 컨테이너 재기동 시 재발하므로 반드시 `reset`으로 리비전을 되돌린다.

## 장애 유형

| 유형 | 주입 엔드포인트 | 리셋 엔드포인트 | 원인 지표 알람 |
|------|---------------|---------------|-----------|
| DB 커넥션 누수 | `POST /fault/db-leak` | `POST /fault/db-leak/reset` | `RcaAgentDev-Healthcare-RdsHighConnections` (임계치: 12) |
| High CPU | `POST /fault/high-cpu` | `POST /fault/high-cpu/reset` | `RcaAgentDev-Healthcare-HighCPU` (임계치: 80%) |
| High Memory | `POST /fault/high-memory` | `POST /fault/high-memory/reset` | `RcaAgentDev-Healthcare-HighMemory` (임계치: 80%) |
| Slow Query | `POST /fault/slow-query` | `POST /fault/slow-query/reset` | 알람 없음 — 증상으로 이어지지 않아 진입점이 아니다 |

RCA **진입 알람**은 `RcaAgentDev-Healthcare-VitalIngestFailures`다. 증상 지표에 걸려
있어 원인을 누설하지 않는다. 위 원인 지표 알람은 에이전트가 검증 단계에서 스스로
찾아야 하는 증거다.

모든 엔드포인트는 `POST` 메서드, `Content-Type: application/json` body: `{"count": N}` (기본값 20 — 커넥션 풀 상한 15를 넘겨야 증상까지 도달한다).

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
| db-leak | 20 | 기본값. 풀 상한 15와 임계치 12를 모두 넘긴다 |
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
  --alarm-names "RcaAgentDev-Healthcare-VitalIngestFailures" "RcaAgentDev-Healthcare-RdsHighConnections" "RcaAgentDev-Healthcare-HighCPU" "RcaAgentDev-Healthcare-HighMemory" \
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
- 직접 API 호출은 주입 후 알람 트리거까지 약 2-3분 소요.
- 배포 기반 주입은 롤아웃과 점진적 누적을 거쳐 약 5-10분 소요.
- 테스트 후 반드시 리셋하여 서비스를 정상 상태로 복원할 것. 배포 기반으로 주입했다면
  `inject_deployment_fault.py reset`으로 태스크 정의까지 되돌려야 한다.
- 배포 기반 주입은 태스크 정의 리비전을 누적시킨다. 오래된 리비전은 주기적으로 정리한다.
