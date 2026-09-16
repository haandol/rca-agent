#### playbook.json

플레이북은 **실행의 근거**다. 사용자가 이 절차를 승인하면 별도 실행 에이전트가
`execution_steps`를 순서대로 수행한다. 따라서 사람이 읽는 서술과 실행 가능한
구조화 절차를 함께 담는다.

```json
{
  "stage": "PLAYBOOK",
  "playbook_id": "UUID",
  "failure_type": "장애 유형",
  "symptom_pattern": "정량 임계치를 포함한 증상 패턴",
  "severity_criteria": "low/medium/high/critical 정량 기준",
  "related_metrics": ["namespace/metric/dimensions와 정상·장애 범위"],
  "verification_steps": [
    "검증 메트릭, 관측 조건, 기준값, Pass, Fail, 에스컬레이션을 포함한 단계"
  ],
  "execution_steps": [
    {
      "step_id": "step-1",
      "intent": "이 단계가 무엇을 달성하려는지",
      "action": "수행할 작업을 자연어로 — 대상 리소스와 조작 내용을 명시",
      "success_criteria": "성공을 판정할 관측 기준 — 어떤 지표가 어떤 값이 되면 성공인지",
      "commands": ["현재 관측으로 대상·리전을 고정한 완성된 AWS CLI 명령"],
      "metric_wait": null
    }
  ],
  "temporary_mitigation": "즉시 증상을 완화하는 조치와 한계",
  "permanent_remediation": "영구 개선 권고",
  "escalation_criteria": "실행 실패 또는 관측 지연 시 에스컬레이션 기준",
  "prevention_measures": ["재발 방지 항목"],
  "tags": ["lowercase-kebab-case"],
  "verification_status": "DRAFT",
  "summary": "플레이북 생성 완료",
  "output_summary": "장애 유형과 실행 절차 요약"
}
```

### execution_steps 규칙

- **`step_id`는 안정적인 식별자**다. 실행 증거가 어느 단계에서 실패했는지 지목하고
  회고가 그 단계를 교정하므로, 순서를 바꾸더라도 기존 식별자를 재사용하지 않는다.
- **`action`은 사람이 읽는 설명이고 `commands`는 실행할 완성된 AWS CLI 명령 목록이다.**
  대상과 리전 인자를 현재 사고 증거로 승인 전에 고정한다. 위 JSON의 설명 문자열을
  실제 명령으로 대체한다. 변수, 자리표시자, 명령 치환, 실행 중 인자 교정은 금지한다.
- 각 단계는 비어 있지 않은 `commands: string[]`, `deployment_wait: dict`, `metric_wait: dict` 중 하나만
  가진다. 관측 연산이면 `commands: []`다. `metric_wait`에는 기존 도구 인자를
  그대로 담되 `step_id`는 제외한다: `action_step_id`(앞선 조치 단계), `metrics`,
  `failure_alarm_name`, `region`, `max_wait_seconds`(1–900, 기본 900), 선택
  `latency_alarm_name`, `completed_work_evidence`. `metrics`는 attempts/failures 필수,
  latency 선택이며 각각 namespace, metric_name, dimensions(Name-to-Value 객체)를 가진다.
  메트릭은 같은 namespace/dimensions를 사용하고 latency와 그 알람은 함께 제공한다.
  list-metrics·describe-alarms로 좌표를 확인할 고정 명령 단계도 관측 연산 전에 포함한다.
  서버만 조치 완료 시각에 첫 두 완결된 60초 구간을 결합한다. 좌표·시간을 추정하지 않는다.
- 새 RCA는 과거 명령과 대상을 상속하지 않고 전체 실행 계획을 교체한다. 빈 목록도
  전체 교체다. 비실행 유형 지식만 보존·병합한다. 현재 근거로 완성된 계획을 만들 수
  없으면 `execution_steps: []`와 수동 에스컬레이션 사유를 남긴다. legacy 자연어에서
  명령을 추정하지 않는다. 승인 후 명령·대상·관측 인자 변경은 새 승인이 필요하다.
- **`success_criteria`는 관측 가능해야 한다.** "정상화됨" 같은 서술 대신 어떤 지표가
  어떤 범위로 돌아오면 성공인지 쓴다. 이 기준이 없으면 실행 에이전트가 이슈 해소를
  판정할 수 없다.
- **되돌릴 수 없는 조치를 담지 않는다.** 리소스·데이터·스냅샷 삭제, 인스턴스 종료,
  자격 증명 회수, 계정·조직 수준 변경은 실행 계층이 거부하므로 그 단계는 수동 조치로
  남는다. 그런 조치가 필요하면 `permanent_remediation`에 권고로 쓴다.
- 확정 근본원인이 없으면 `execution_steps`를 비운다. 미확정 원인에 대한 추측 절차는
  실행 근거가 될 수 없다.
- 서버가 전달한 실행 능력과 발견된 소유자에 근거해 읽기 전용 대상 확인 → 승인된 조작 →
  사후 건강 관측 순서로 작성한다. 직접 SQL·셸·ECS exec 또는 새 태스크 등록/실행을 요구하지
  않는다. 실재하지 않는 스케줄러·환경 플래그·명령 인자를 만들지 않는다.
- 독립 실행 태스크의 소유권과 정상 중지 시 롤백 경로가 확인되면 해당 태스크의 중지를
  고정된 명령으로 제안할 수 있다. 이는 영구 인스턴스 종료가 아니며 실제 명령은 기존 서버 gate가
  판정한다. 소유자나 실행 능력이 불명확하면 그 조작은 수동 에스컬레이션으로 남긴다.
- 태스크 STOPPED나 알람 OK만으로 성공을 판정하지 않는다. 승인된 조작과 연결된 해제 기록,
  실제 성공 요청과 회복 지표를 요구한다. 자연 만료나 관측 부재는 성공 근거가 아니다.

`verification_status`는 항상 `DRAFT`로 쓴다. 이 플레이북은 아직 실행되지 않았으므로
절차의 정확성이 검증되지 않았다. 실행과 회고를 거친 뒤 서버가 이 값을 갱신한다.

위 스키마의 설명 문자열과 지식 필드는 필수다. 명령 단계의 metric_wait는 null이고
관측 단계의 commands는 빈 목록이다. 필수 설명 문자열이 없거나 비면 저장이 거부되고
플레이북은 기록되지 않는다. 특히 `severity_criteria`, `escalation_criteria`,
`symptom_pattern`을 생략하지 않는다. 저장 도구가 `ok: false`를 반환하면 지적된
필드를 채워 같은 파일을 다시 저장한다.

실행 결과와 정상화 여부는 이 플레이북에 쓰지 않는다. 실행은 사용자 승인 뒤에
일어나므로 작성 시점에는 알 수 없다.

### 승인된 서비스 배포와 수렴

각 단계는 commands / deployment_wait / metric_wait 중 하나만 가진다. 서비스 롤백은
정상 저장이 입증된 서버 소유 `rollback_context` 사본을 보유해야 한다.
loader가 원본·지문·대상·시각을 검증한 기록만 사용하며 모델이 검증 표식을 만들지 않는다.
정상 기준이 없거나 현재 결함 대상과 일치하지 않으면 실행 단계를 만들지 않는다.

롤백 commands는 `aws ecs update-service --cluster ... --service ... --task-definition ...
--region ...` 하나이며 추가 옵션을 넣지 않는다. 같은 단계의 `ecs_service_precondition`은
account_id, region, cluster(ARN), service(ARN), container_name, desired_count,
expected_task_definition(결함 ARN), expected_image_digest(sha256), expected_deployment_id,
service_settings를 모두 가진다. service_settings의 키는 deploymentConfiguration,
networkConfiguration, capacityProviderStrategy, launchType, platformVersion, schedulingStrategy다.
관측에 없는 값의 기본값은 각각 {}, {}, [], null, null, REPLICA다.

다음 `deployment_wait`는 action_step_id(롤백 단계), account_id, region, cluster, service,
container_name, desired_count, task_definition(정상 ARN), image_digest(정상 sha256),
max_wait_seconds(1..900)를 모두 가진다. 앱 컨테이너와 사이드카를 구분하며 실제 앱의
정상 digest·health, 모든 서비스 태스크의 전환과 이전 장애 버전 소멸을 서버가 검사한다.
AWS 일반 wait 명령을 대신 넣거나 승인 범위 밖 조회·시간을 추가하지 않는다.

배포 후 metric_wait는 기존 action_step_id 대신 deployment_step_id(수렴 단계)를 가진다.
두 참조는 XOR이다. 나머지 기존 지표·알람·완료 증거 입력은 같다. 최초 수렴 UTC 시각의
다음 분부터 첫 두 완결된 60초 구간만 사용한다. StopTask의 기존 action_step_id는 유지한다.
배포 수렴과 지표 도착 대기는 각각 최대 900초이며 실행·도구 예산을 늘리지 않는다.

rollback_context는 서버의 검증된 reader가 baseline_ref, scope, normal, current,
service_settings를 포함해 붙인다. 모델 출력에서 이 문맥을 생성·채택·수정하지 않는다.
baseline_ref는 bucket, key, sha256이다. scope는 account_id, region, cluster_arn,
service_arn, service_name, container_name, desired_count, log_group이다. normal은
task_definition_arn, image_digest이며 current는 같은 두 값과 deployment_id다.
명령과 수렴 목표는 normal, 전제는 current, 모든 좌표와 설정은 scope/service_settings와
같아야 한다. 실제 정상 저장·원본·지문·시간 검증은 reader의 책임이며 실행 워커는
승인 문맥만 소비한다. 문맥이 있는 UpdateService에서 전제나 수렴 단계를 제거할 수 없다.
