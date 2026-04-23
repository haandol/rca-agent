---
name: evidence-patterns
description: AWS 서비스별 증거 수집 패턴 — CloudWatch 메트릭 조회 전략, Logs Insights 쿼리 템플릿, CloudTrail 이벤트 필터. 초기 스코핑 단계와 증거 수집 단계에서 어떤 메트릭을 어떤 순서로 수집해야 하는지 판단할 때 반드시 이 스킬을 참조한다. ECS, RDS, Lambda, ALB 등 서비스 이름이 언급되거나, 메트릭 조회·로그 검색·변경 이력 조회를 수행하려 할 때 사용한다.
---

# 증거 수집 패턴

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
