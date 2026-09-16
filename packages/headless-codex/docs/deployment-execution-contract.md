# 승인 서비스 롤백 실행 계약

Agent의 검증된 reader가 `playbook.rollback_context`를 붙인다. 이 필드는 모델 출력이
아니며 승인 사본과 지문에 포함한다. reader가 S3 원본의 허용 위치·내용 지문·서비스·
실행·장애 이전 정상 저장을 검증한다. 실행 워커는 S3를 다시 읽거나 정상 대상을
추정하지 않고 승인 사본과 생성된 명령·전제를 대조한다.

아래는 정확한 필드와 타입이다. `string`은 비어 있지 않은 고정 문자열이다.

```text
rollback_context: {
  baseline_ref: {bucket: string, key: string, sha256: 64 lowercase hex (optional sha256: prefix)},
  scope: {
    account_id: 12 digits, region: string,
    cluster_arn: ECS cluster ARN, service_arn: ECS service ARN,
    service_name: string, container_name: string,
    desired_count: positive integer, log_group: string
  },
  normal: {task_definition_arn: revision ARN, image_digest: sha256:<64hex>},
  current: {
    task_definition_arn: revision ARN, image_digest: sha256:<64hex>,
    deployment_id: string
  },
  service_settings: {
    deploymentConfiguration: object,
    networkConfiguration: object,
    capacityProviderStrategy: array,
    launchType: string|null,
    platformVersion: string|null,
    schedulingStrategy: REPLICA
  }
}
```

위 객체들은 지정된 키만 가진다. 서비스 설정은 실제 DescribeServices 결과의 고정 투영이다.
누락 필드 기본값은 순서대로 `{}`, `{}`, `[]`, `null`, `null`, `REPLICA`다.

각 execution_steps 항목은 기존 `step_id`, `action`, `success_criteria`와 선택 `intent`를
유지하고 `commands` / `deployment_wait` / `metric_wait` 중 하나만 가진다.
비활성 commands는 생략 또는 []이며 비활성 대기 필드는 생략 또는 null이다.

```text
rollback step:
  commands: ["aws ecs update-service --cluster <clusterArn> --service <serviceArn> --task-definition <normalTDArn> --region <region>"]
  ecs_service_precondition: {
    account_id, region, cluster, service, container_name, desired_count,
    expected_task_definition, expected_image_digest, expected_deployment_id,
    service_settings
  }

convergence step:
  deployment_wait: {
    action_step_id: prior rollback step ID,
    account_id, region, cluster, service, container_name, desired_count,
    task_definition: normal TD ARN, image_digest: normal digest,
    max_wait_seconds: integer 1..900
  }

post-convergence metrics step:
  metric_wait: {
    deployment_step_id: prior convergence step ID,
    metrics, failure_alarm_name, region,
    max_wait_seconds?: integer 1..900 (default 900),
    latency_alarm_name?: string,
    completed_work_evidence?: object
  }
```

`cluster`와 `service`는 ARN이며 context의 cluster_arn/service_arn과 같다.
롤백 명령은 정확한 문자열로 승인하며 옵션은 cluster/service/task-definition/region만
한 번씩 가진 일반 argv다. guard는 current, 명령과 수렴 목표는 normal과 같다.
같은 fault 태스크 정의로 외부에서 재배포한 경우도 expected_deployment_id로 차단한다.
context가 있는 UpdateService에서 guard를 제거하면 승인·실행 검증이 거부한다.
문맥 없는 기존 일반 명령은 이 기능 때문에 추가로 거부하지 않는다.

metric_wait의 기존 action_step_id는 StopTask를 참조한다. action_step_id와
새 deployment_step_id는 XOR이며 나머지 지표 계약은 같다. metrics는 필수 attempts/failures와
선택 latency이며 각각 namespace, metric_name, dimensions(이름→값 객체)다. latency와
latency_alarm_name은 함께 제공한다. completed_work_evidence는 기존 서버 검증 참조를 사용한다.

실행 도구는 `run_playbook_command(step_id, command)`와
`wait_for_service_deployment(step_id)`, `wait_for_post_action_metrics(...)`다.
서버는 쓰기 직전에 target TD의 유효성, 현재 서비스·배포 ID·앱 digest·health·설정을
실제로 조회한다. 이 조회와 전제 영수증을 보존한 뒤 취소·claim·기한을 다시 확인하고 쓴다.
UpdateService의 실제 응답에 있는 대상과 신규 배포 ID도 대조한다.

수렴은 신규 배포가 유일한 정상 PRIMARY이고 실제 앱 태스크가 모두 정상 TD/digest/health로
전환됐으며 사전에 기록한 장애 태스크도 STOPPED일 때만 기록한다. 사이드카의 digest를
앱 digest 대신 사용하지 않는다. 배포 ID·설정이 바뀌면 중단하며 API 성공을 수렴으로 세지 않는다.

대기는 시작 요청·기한·관측·최초 수렴 시각을 보존한다. 동일 요청은 종결 영수증을 재사용하고,
중단된 요청은 새 기한을 얻지 않는다. 배포와 지표 대기는 각각 최대 900초이며 기존 실행과
도구 1200초 한도 안에서 수행한다. subprocess 정리 시간도 남은 예산에 포함한다.

지표 구간은 최초 수렴 시각이 속한 분의 다음 분부터 첫 두 완결된 UTC 60초 구간이다.
정확한 분 경계에서 수렴해도 다음 분부터 시작한다. 지표 대기 중에도 같은 배포가 유지되는지
검사하고 바뀌면 중단한다. 각 구간 시도 양수·실패 0·실패 알람 OK와 실제 완료된 쓰기 증거는
기존 성공 기준대로 별도로 확인한다. 지표 영수증만으로 전체 실행이 해결되지는 않는다.

통합 담당자는 Agent의 runbook_contract.py 사본을 headless-codex 사본과 동일하게 맞추고,
Dashboard가 새 필드 및 reader 소유 rollback_context를 승인 내용에 보존하도록 연결한다.
AWS 권한 변경과 배포는 이 패키지 작업에 포함하지 않는다.
