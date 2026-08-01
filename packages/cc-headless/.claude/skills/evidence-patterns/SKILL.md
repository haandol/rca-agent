---
name: evidence-patterns
description: AWS 서비스별 증거 수집 패턴 — CloudWatch 메트릭 조회 전략, Logs Insights 쿼리 템플릿, CloudTrail 이벤트 필터. 초기 스코핑 단계와 증거 수집 단계에서 어떤 메트릭을 어떤 순서로 수집해야 하는지 판단할 때 반드시 이 스킬을 참조한다. ECS, RDS, Lambda, ALB 등 서비스 이름이 언급되거나, 메트릭 조회·로그 검색·변경 이력 조회를 수행하려 할 때 사용한다.
---

# 증거 수집 패턴

## 증거 시간 범위

조사 시작 시 원본 알람의 상태 변경 시각과 메트릭 period를 사용해 current alarm
window의 시작·종료를 ISO-8601로 고정한다. baseline은 동일 길이의 historical
comparison window로 별도 조회한다. 메트릭·로그·변경 이벤트마다 window와 실제 관측
시각을 기록한다.

current alarm window보다 앞선 수동 테스트·수동 장애 주입 로그는 historical
context로만 사용할 수 있다. 시각이 없거나 어느 window인지 판별할 수 없는 로그를
현재 장애의 발생, 원인, 지속 증거로 사용하지 않는다.

## 관측 결과를 기록하는 형태

`scoping.json`은 `metric_observations`와 `concurrent_alarms` 두 배열을 항목이 없어도
반드시 포함한다.

```json
{
  "metric_observations": [
    {
      "metric_name": "DatabaseConnections",
      "datapoints": [2, 12, 20, 27, 30],
      "trend": "rising",
      "shape_note": "",
      "window_start": "2026-08-01T00:00:00Z",
      "window_end": "2026-08-01T00:30:00Z",
      "unit": "Count",
      "baseline": 2
    }
  ],
  "concurrent_alarms": [
    { "alarm_name": "RcaAgentDev-Healthcare-VitalIngestFailures", "state": "ALARM" }
  ]
}
```

- **`datapoints`는 조회한 값을 시간 순서대로 담는다.** 현재 값과 기준선 두 숫자로
  요약하지 않는다. 두 형태는 현재 값이 같아도 원인이 다르다 — 계속 오르는 지표는
  누수이고, 튀었다 돌아온 지표는 일시적 부하다. 두 숫자만 남기면 이 구별이 사라지고
  가설 검증 단계가 그것을 복원할 수 없다.
- `trend`는 `rising`·`falling`·`flat`·`spike`·`unknown` 중 하나로 **당신이 시퀀스를 읽은
  결과**를 쓴다. 데이터포인트가 2개 미만이면 형태를 알 수 없으므로 `unknown`을 쓰며,
  적은 데이터포인트로 추세를 단정하면 게이트가 거부한다.
- **어휘에 담기지 않는 형태는 `shape_note`에 서술한다.** 계단식 상승, 톱니형, 주기적
  진동처럼 다섯 항목에 들어맞지 않는 패턴을 가장 가까운 항목으로 뭉개지 않는다. 어휘는
  요약이고 `datapoints`가 근거이므로, 하류 단계는 시퀀스를 읽어 다르게 해석할 수 있다.
- **확인한 동시 발생 알람은 전부 기록한다.** 확인했는데 적지 않으면 하류 단계에는
  발화하지 않은 알람으로 읽히고, "다른 알람은 없었다"는 반대 서술이 근거 없이
  성립한다. 없으면 빈 배열을 쓴다.

## 서비스별 메트릭 수집 패턴

### ECS Fargate 서비스

필수 메트릭:
- AWS/ECS: CPUUtilization (ServiceName, ClusterName)
- AWS/ECS: MemoryUtilization (ServiceName, ClusterName)
- AWS/ECS: RunningTaskCount (ServiceName, ClusterName)
- AWS/ECS: DesiredTaskCount (ServiceName, ClusterName)

조회 전략:
1. 알람 메트릭을 먼저 조회 (30분 윈도우, Period=60)
2. 24시간 전 동일 구간과 비교
3. CPU > 80%일 때 MemoryUtilization 함께 확인
4. RunningTaskCount < DesiredTaskCount이면 태스크 시작 실패 의심

### RDS / Aurora

필수 메트릭:
- AWS/RDS: CPUUtilization (DBInstanceIdentifier)
- AWS/RDS: FreeableMemory
- AWS/RDS: DatabaseConnections
- AWS/RDS: ReadLatency, WriteLatency
- AWS/RDS: FreeStorageSpace

조회 전략:
1. DatabaseConnections 급증 → 커넥션 누수 의심
2. CPUUtilization + ReadLatency 동시 상승 → 비효율 쿼리 의심
3. FreeableMemory 급감 → OOM 또는 쿼리 버퍼 과다 사용

### Lambda

필수 메트릭:
- AWS/Lambda: Duration (FunctionName)
- AWS/Lambda: Errors
- AWS/Lambda: Throttles
- AWS/Lambda: ConcurrentExecutions
- AWS/Lambda: IteratorAge (스트림 트리거 시)

조회 전략:
1. Duration 급증 → 다운스트림 지연 또는 콜드 스타트 의심
2. Errors + Throttles 동시 → 동시성 제한 도달
3. IteratorAge 증가 → 처리 속도 < 이벤트 발생 속도

### ALB / NLB

필수 메트릭:
- AWS/ApplicationELB: TargetResponseTime
- AWS/ApplicationELB: HTTPCode_Target_5XX_Count
- AWS/ApplicationELB: HealthyHostCount, UnHealthyHostCount
- AWS/ApplicationELB: RequestCount

조회 전략:
1. 5XX 급증 → 백엔드 장애, 타겟 헬스체크 확인
2. TargetResponseTime 급증 + 정상 RequestCount → 백엔드 지연
3. UnHealthyHostCount > 0 → 타겟 장애

## CloudWatch Logs Insights 쿼리 패턴

### 에러 패턴 검색

```
fields @timestamp, @message
| filter @message like /(?i)(error|exception|timeout|refused|fatal)/
| sort @timestamp desc
| limit 50
```

### 특정 에러 코드 집계

```
fields @timestamp, @message
| filter @message like /(?i)error/
| parse @message /(?<errorType>[\w]+Error|[\w]+Exception)/
| stats count() by errorType
| sort count() desc
```

### 느린 요청 분석

```
fields @timestamp, @message
| filter @message like /(?i)(duration|latency|slow)/
| parse @message /duration[=: ]*(?<duration>\d+)/
| filter duration > 1000
| sort @timestamp desc
| limit 20
```

## CloudTrail 이벤트 패턴

### 배포 관련 이벤트

EventName 필터:
- UpdateService (ECS 배포)
- RegisterTaskDefinition (새 태스크 정의)
- UpdateFunctionCode (Lambda 코드 업데이트)
- UpdateFunctionConfiguration (Lambda 설정 변경)
- CreateDeployment (CodeDeploy)

### 설정 변경 이벤트

EventName 필터:
- PutScalingPolicy (오토스케일링 정책)
- ModifyDBInstance (RDS 인스턴스 설정)
- ModifyDBCluster (Aurora 클러스터 설정)
- UpdateItem (DynamoDB 설정 변경)
- PutBucketPolicy (S3 정책 변경)

### IAM / 보안 이벤트

EventName 필터:
- PutRolePolicy, AttachRolePolicy, DeleteRolePolicy
- CreateAccessKey, DeleteAccessKey
- AssumeRole 실패 (errorCode: AccessDenied)
