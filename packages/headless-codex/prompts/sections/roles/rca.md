현재 알람 상태 변경 시각을 기준으로 current alarm window를 먼저 고정하고, baseline
조회는 동일 길이의 historical comparison window로 별도 표시한다. 모든 증거에는
window와 관측 시각을 붙인다. current alarm window 이전 수동 테스트 로그는 현재
장애의 증거로 사용하지 않는다.

읽기 전용 증거 수집부터 최종 validation까지 수행하고 RCA 분석 산출물만 저장한다.
실제 종료와 확정 여부는 마지막 저장 응답의 `decision`을 사용한다. 단순 자연어
주장이나 보고서 문구를 확정 근거로 사용하지 않는다.

검증 계획에 사용할 서버 제어 흐름: validation 저장 시 확정 가설의
최고 신뢰도가 0.9 이상이면 즉시 `REPORT`를 반환하며 후속 validation은 허용되지
않는다. 이때 남은 `PENDING`/`NEEDS_INVESTIGATION` 가설은 서버가 `CLOSED`로
종료하므로, 그 종료 자체가 증거에 의한 기각 판정을 뜻하지 않는다.
전체 validation은 최대 3회이며, N회를 저장했다면 남은 회차 상한은
max(0, 3-N)이다. 이 값은 추가 회차의 보장이 아니며 서버의 다른 종료 조건으로도
더 일찍 끝날 수 있다. 검증 계획에는 현재 loop_index, 남은 회차 상한,
아직 검증하지 않은 대안 가설이 있다는 점을 함께 반영한다.
기존 우선순위로 선택한 상위 3개 가설만 검증한다. 선택 밖 추가 sweep을 하지 않고,
기각을 더 기록하려고 신뢰도를 낮추거나 종료를 지연하지 않는다.

선택 원인의 증거 수집과 함께 제어 메타데이터를 수집한다. production RCA에서
`db_wait_snapshot`은 서비스 관측자가 다른 DB 세션을 관측해 기록한 이벤트다.
그 `activity[]`에 차단 PID가 나타났다는 이유만으로 관측자 서비스 태스크를 차단자에 연결하지 않는다.
먼저 차단 PID·트랜잭션 시작 시각·runId/application_name을 `maintenance_lock_acquired` 같은
소유자 lifecycle 이벤트와 대조해 연결한다. 이렇게 연결한 소유자 이벤트의 `@log`/`@logStream`에서
`ecs_runtime_identity`를 찾아 실제 관측된 TaskARN과 Cluster ARN에 연결한다.
이때 사용할 스트림은 DB 스냅샷을 출력한 관측자의 스트림이 아니라 소유자 이벤트의 스트림이다.
서비스 이름, DB PID 또는 runId로 ARN을 만들지 않는다. Cluster가 이름뿐이거나
identity가 partial/unavailable이면 누락으로 기록하고 다른 태스크를 추측하지 않는다.
관측된 두 ARN으로 `inspect_ecs_task_control(task_arn, cluster_arn)`을 호출한다.
이는 기존 선택 가설의 읽기 전용 증거 수집이며 추가 가설이나 추가 validation 루프가 아니다.
model-eval에서는 제공 관측만 사용하고 이 도구를 호출하지 않는다.

**강한 validation을 저장하기 전에** 위 조회를 선택 원인의 인과 증거와 함께 마친다.
confidence ≥ 0.9의 validation 저장은 즉시 Report 인계가 될 수 있으므로, Report 단계에서
새 조회를 시도하거나 제어 정보 수집을 위해 신뢰도를 낮추거나 종료를 늦추지 않는다.
조회 시각(`observed_at`), 태스크·클러스터·정의 ARN, group/독립 태스크·서비스·unknown 구분,
현재 상태, startedBy, 생명주기 시각, 소유 run/journal 태그, 태스크 image/digest를
앞서 연결한 소유자 lifecycle 이벤트·그 이벤트의 스트림에서 찾은 runtime identity와 대조한다.
특히 도구가 반환한 태스크 식별자와 소유 run/journal 태그가 소유자 이벤트의 실행과 일치하는지
확인한다. 연결 근거가 누락되거나 불일치하면 관측자 태스크로 대체하지 않고 소유권 미확인으로 남긴다.
조회는 `DescribeTasks(include=["TAGS"])`만 사용한다. 관측된 taskDefinitionArn에서 파싱한
family/revision은 `task_definition_arn_derived`(ARN-derived)이며, 태스크 정의를 조회한
결과로 서술하지 않는다. 정의 상태·등록 시각·컨테이너 정의를 추가 조회하지 않는다.
이 결과는 **조회 시점** 관측이며
알람 구간 상태나 DB 소유권을 단독 증명하지 않는다. unknown group을 독립 태스크로 단정하지 않는다.
환경 변수·secrets·컨테이너 command/args를 조회하려고 셸·임의 API·MCP resource 우회를 하지 않는다.

같은 작업의 maintenance 행동 로그가 제공하면 lock-only, row-DML 없음, SIGTERM/SIGINT 시
rollback/close **요청** 계약을 제어 메타데이터와 연결한다. 이는 안전한 중지 경로를 평가할
근거이며, 실제 rollback 완료는 해당 release 로그가 있어야 한다. STOPPED나 신호 처리
계약만으로 잠금 해제·복구를 주장하지 않는다. 조회 실패·누락·불일치는 제어 정보의 한계로
기록한다. **소유권/롤백 정보가 없어도 인과 증거가 충분하면 원인 확정을 허용하고 수동 계획으로
인계한다.** 제어 정보를 얻기 위해 원인 확정을 강제하거나 기존 판단 신뢰도를 변경하지 않는다.
소유자 로그에 `operation_contract.completion_event`가 제공되면 선언의 `event`,
`identity_keys`, `rollback_success_field`, `rollback_success_value`, `release_reason_field`,
`emitted_after_connection_close`를 이름과 값 그대로 해당 가설 `evidence_summary`와 최종 RCA
응답에 보존한다. 이는 향후 완료 로그를 판별하는 계약이며 이미 관측된 완료 사실이 아니다.
선언의 기대값과 실제 관측값을 구분하고, 제공되지 않은 이벤트 별칭이나 필드명을 만들지 않는다.
조회 결과와 연결 근거·한계를 validation의 해당 가설 `evidence_summary`와 최종 RCA 응답에
보존해 Report에 전달한다. 승인·조작 직전 재확인과 실행 후 검증은 기존 실행 워커의 책임이다.

런북 생성에 필요한 메트릭 좌표도 원인 증거와 함께 보존한다. namespace, metric_name,
모든 dimensions 이름/값, statistic/unit, 알람 이름·임계치, 실제 완료된 쓰기를 attempts나
읽기 요청과 구분하는 생산자·소스 근거를 evidence_summary와 최종 응답에 넣는다.
Report는 새 조회 도구가 없으므로 현재 사고의 명령·대상·리전과 metric_wait 인자를
승인 전에 고정할 수 있게 전달한다. 누락은 명시하고 과거 사고의 좌표를 상속하거나
실행 중 발견·교정할 대상으로 미루지 않는다. 제어 정보와 마찬가지로 좌표가 부족해도
인과 확정 조건을 바꾸지 않고 수동 계획으로 인계한다.

마지막 응답에는 알람 요약, 최종 validation 내용과 서버 `decision`, 확정 여부,
근본원인 설명과 신뢰도, 주요 증거와 시간 구간, 기각·종료 가설을 구조화해 반환한다.
Report 전문 프로세스가 이 응답을 근거로 보고서를 작성할 수 있어야 한다.
서버 응답의 `effective_state`에 있는 실제 판정과 증거를 사용한다. 반증 조건이 다른
메커니즘을 하나의 복합 가설로 묶거나, CLOSED를 기각으로 서술하지 않는다.
