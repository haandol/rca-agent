# 회고 검토와 공용 반영 데이터 연결

2026-09-19 작성. 현재 구현의 embedded 관련 런북 wire를 기록한다. 새 정책을 정의하는
문서가 아니며 상태·권한 계약은 아래 Related ADRs가 소유한다. 2026-09-19 core freeze의
소스와 시간 필드·최소 원본 만료 계산을 대조했다. 구현 전체의 검토 판정은 별도다.

공용 플레이북의 일반 지식과 관련 사고의 런북은 한 산출물 안에서 구분된다. 모델은
일반 지식을 작성하고 서버가 검증한 정확한 `execution_steps`와 `rollback_context`를
관련 런북 필드에 연결한다. 이 연결은 새 실행 승인이나 다른 사고에 대한 명령 보증이 아니다.
현재 wire는 별도 `bound_runbook_*` 필드를 사용하지 않는다.

과거의 빈 관련 런북 표현은 `association_mode=LEGACY_SAME_GENERATION`으로만 호환한다.
이는 새 실행 권한이 아니라 기존 완료 세대의 typed 연결을 검증한 결과다. 아래의
`legacy_association`은 기존 공개 원문을 덮지 않고 새 retrospective 개정의 출처로 남는다.

## 기존 테이블의 접근 경로

별도 DynamoDB 테이블이나 승인 큐를 만들지 않는다. 아래 세 항목은 기존 RCA 세션
테이블에 저장한다. 전체 승인·실행 증거·회고 JSON은 S3에 있고 항목에는 참조와 지문을 둔다.

| 용도 | PK | SK | 조회/쓰기 |
|---|---|---|---|
| 실행별 보존 회고의 후속 반영 | `RCA#{rca_id}` | `RETROSPECTIVE_FOLLOWUP#{execution_id}#{review_sha256}` | 동일 실행·동일 회고의 child를 일관 읽기; 상태/lease를 조건부 갱신 |
| 대기 작업 발견 | `RETROSPECTIVE_PENDING` | `{rca_id}#{execution_id}#{review_sha256}` | 고정 파티션 Query; 실제 child를 읽어 처리 |
| 조기 런북과 정식 공용 기준의 불변 연결 | `RCA#{rca_id}` | `RECOVERY_PUBLICATION#{recovery_revision}` | publisher가 조건부 생성; consumer가 동일 개정의 연결을 일관 읽기 |

`review_sha256`는 S3 회고 문서의 실제 바이트 SHA-256이다. 따라서 동일 실행의 동일
보존 검토는 같은 child 키를 사용한다. 이 키를 보고 새로운 모델 회고를 실행하지 않는다.
과거 실행의 `EXEC#{execution_id}`와 그 `FAILED` 결과는 child 생성으로 덮어쓰지 않는다.

## `RETROSPECTIVE_FOLLOWUP`

| 필드 | 현재 wire의 의미 |
|---|---|
| `PK`, `SK` | 위 실행별 child 키 |
| `record_type` | `RETROSPECTIVE_FOLLOWUP` |
| `schema_version` | 현재 값 `1` |
| `attempt_id` | 해당 child를 식별하는 `review_sha256` |
| `rca_id`, `execution_id`, `engine` | 원 실행의 사고·실행·분석 출처. `engine`은 `strands` 또는 `headless-codex` |
| `approval_id`, `playbook_digest` | 승인 식별자와 승인 S3 사본의 실제 바이트 SHA-256 |
| `approved_playbook_s3_key` | `approvals/{rca_id}/{approval_id}/playbook.json` |
| `source_part_revision` | 실행이 승인받은 recovery 개정. 원 실행의 `source_part_payload_sha256`와 같아야 함 |
| `recovery_playbook_sha256` | 승인 런북 객체의 정규 JSON 내용 지문. 승인 바이트 지문과 구분 |
| `review_s3_key`, `review_sha256` | `executions/{rca_id}/{execution_id}/retrospective-diff.json`과 그 실제 바이트 지문 |
| `evidence_s3_key`, `evidence_sha256` | `executions/{rca_id}/{execution_id}/evidence.json`과 그 실제 바이트 지문 |
| `review_status` | 유효한 보존 검토를 등록할 때 `COMPLETED` |
| `status` | 아래 공용 반영 상태 |
| `reason` | 대기·차단·실패 사유 또는 게시 결과 요약 |
| `created_at`, `updated_at` | 정수 Unix epoch 초. UTC 시각으로 표시 |
| `ttl` | 원 실행의 경량 상태 만료를 이어받는 정수 Unix epoch 초 |
| `body_expires_at` | 승인·실행 증거·회고 S3 객체 각각의 `LastModified + 60일`과 실행 `started_at + 60일`의 최솟값을 정수 Unix epoch 초로 고정. 아래 시간 검증 참조 |
| `pending_key` | 연결된 `RETROSPECTIVE_PENDING` 항목의 `{PK, SK}` |
| `lease_token`, `lease_until` | 처리 중인 소유권 값과 정수 Unix epoch 초 만료. 소유권 값은 화면/진단 출력에 노출하지 않음 |
| `published_revision`, `public_playbook_id` | 게시 완료 시 확정한 공용 개정·플레이북 식별자 |

### 상태와 원래 실행의 관계

- `WAITING_FOR_PUBLICATION`: 검토는 보존됐으나 정식 공용 기준/연결이 아직 준비되지 않음.
- `PUBLISHING`: 해당 child의 lease를 얻어 보존 입력과 현재 기준을 검증·반영 중.
- `PUBLISHED`: 원문·검색 게시 일치와 최종 조건부 확정까지 완료.
- `BLOCKED`: 원본·승인·공용 기준 불일치, 삭제/만료 등으로 반영 불가. 대기 참조를 제거.
- `FAILED`: 게시 작업 실패. 이유를 남기고 보존된 작업을 재처리할 수 있음.

현재 구현은 대기/실패 child 또는 lease가 만료된 처리 중 child를 조건부로 획득한다.
후속 child의 처리 상태가 바뀌어도 원래 `EXEC`의 실패 이력과 승인·실행 증거를 바꾸지
않는다. 각 처리의 동시성·중복 방지와 원문 만료는 별도 guard로 검사한다.

## `RETROSPECTIVE_PENDING`

| 필드 | 의미 |
|---|---|
| `PK`, `SK` | 위 고정 대기 파티션과 실행/검토별 키 |
| `followup_key` | 실제 권위 child의 `{PK, SK}` |
| `ttl` | child의 경량 상태 만료 시각 |

등록은 원 `EXEC`의 해결 상태·승인 지문·검토/증거 참조·recovery 개정·TTL 조건 확인과
child/대기 참조의 생성을 한 트랜잭션으로 수행한다. 대기 참조 자체는 게시 권위가 아니다.
완료 시에는 최종 게시 트랜잭션에서 정확한 child를 가리키는 참조만 제거한다.
정리 중에는 종료된 child의 상태와 참조 일치를 다시 확인한다. 기존 과거 FAILED 발견은
제한된 스캔 경로가 담당하고, 일반 대기 작업 조회는 이 파티션의 Query를 사용한다.

## `RECOVERY_PUBLICATION`

| 필드 | 현재 wire의 의미 |
|---|---|
| `PK`, `SK`, `schema_version` | 위 RCA/recovery 개정 키와 버전 `1` |
| `rca_id`, `engine` | 연결이 속한 사고와 recovery 원본 엔진 |
| `recovery_revision` | recovery의 `revision` 및 `payload_sha256`와 같은 개정 |
| `recovery_playbook_sha256` | 검증된 recovery 런북의 정규 JSON 내용 지문 |
| `public_playbook_id`, `public_revision` | 정확한 공용 식별자·기준 개정. 새 분석 게시에서는 `analysis:{rca_id}`, 사용자 반영에서는 disposition의 `result_revision` |
| `public_body_sha256` | library가 보유한 공용 domain payload의 정규 JSON 내용 지문 |
| `source_rca_id`, `source_engine` | 해당 공용 기준 snapshot의 출처. 승인 출처 엔진과 혼동하지 않음 |
| `created_at` | UTC ISO 8601 문자열. FOLLOWUP의 정수 epoch 형식과 다름 |
| `ttl` | 정수 Unix epoch 초. 신규 분석 publisher는 세션 TTL·공용 snapshot TTL·recovery `body_expires_at`(없으면 recovery TTL)의 최솟값. 사용자 반영 publisher는 아래 시간 검증 참조 |
| `association_mode` | 과거 분리 표현을 검증했을 때만 `LEGACY_SAME_GENERATION`. 일반 embedded 연결에는 없음 |
| `legacy_association` | 완료 부모 key/시각, generation `claim_sha256`, report key/바이트 SHA256, 알림 정규 JSON SHA256, 승인된 private ID와 recovery 원문 key/SHA/digest, attempt와 부모 attempt 필드 존재 여부, 세 terminal part의 typed 참조. claim 토큰 자체의 별도 필드는 저장하지 않음 |

레거시 경로는 부모 `COMPLETED/analysis_parts_finalized`, 같은 generation의 `READY`
recovery, part 완료≤부모 완료, 원문 SHA·예약 revision·승인 digest와 전체 런북 동일성을
요구한다. report key는 정확한 `attempt-{attempt 또는 필드 부재 시 1}-{claim_token}` 경로이며,
알림은 기존 서버 요약 형식으로 대조한다. report의 유일한 서버 summary/private ID,
최종 공개 구역/public ID, terminal manifest 세 줄을 검증하고 세 part 모두 같은
generation/attempt의 보존된 terminal 레코드와 일치해야 한다. root/operations의
`FAILED`·`SKIPPED`도 실제 terminal 결과로 허용하며 hash/시간 검증을 생략하지 않는다.
레거시 TTL은 부모·공개 snapshot·모든 part body 만료·실제 report/recovery S3 원본
만료·child 원본 만료의 최솟값이다. source 조건은 association 생성과 최종 공개 CAS에
함께 포함하며 vector 처리 뒤 원문을 재검증한다. 기존 canonical snapshot과 승인/실행
원문은 유지되고 새 retrospective 개정만 생성된다.

publisher는 보존 recovery의 완료/READY·원본 지문·유효한 런북을 확인한다. 공용 기준의
snapshot/head가 실제 게시된 내용과 같고 현재 분석 claim 및 recovery 개정이 유지될 때
불변 연결을 생성한다. 동일 키의 같은 연결은 재사용하며, 다른 연결로 덮어쓰지 않는다.

consumer는 이 연결로 canonical snapshot을 읽고 `execution_steps`, `rollback_context`,
`region`, `account_id`, `target_*`, `execution_*`, `approval_*`로 표현된 전체 관련 런북을
승인 사본과 비교한다. 서로 다른 엔진의 공개 출처와 현재 완료 분석의 게시 엔진을 별도로
검증하며 engine 문자열 하나로 계보를 대신하지 않는다.

## 시간 필드와 만료 검증

FOLLOWUP의 `created_at`, `updated_at`, `ttl`, `body_expires_at`, `lease_until`은 Unix epoch
초이며 RECOVERY_PUBLICATION의 `created_at`만 UTC ISO 8601 문자열이다. 원래 실행의
`started_at`은 시간대가 있는 ISO 8601 값이어야 한다.

등록 시 reader는 승인 사본·실행 evidence·회고 diff를 각각 읽어 S3 `LastModified`와
60일을 더한 시각을 확인한다. `body_expires_at`은 이 세 값과 실행 시작 후 60일의
최솟값이다. 후속 등록 시각으로 원본 기한을 새로 시작하지 않는다. 재개 때 같은
원본들을 다시 읽어 지문과 최소 만료값이 등록된 값과 정확히 같은지 확인한다.
원본 읽기의 60일 경계, 후속 상태 TTL, 원본 기한 중 하나라도 경과하면 반영하지 않는다.

`_lease_guard`가 적용되는 staging/개정 확정과 벡터 쓰기 전후에는 현재 시각으로
lease·상태 TTL·`body_expires_at`을 대조한다. 최종 library 확정 트랜잭션도 이 세 기한을
다시 검사한다. 기준 비교 때 유효했더라도 벡터 처리 중 원본 기한이 지나면 완료로
확정할 수 없다. lease의 300초는 현재 작업 소유권 설정이며 원본·상태 보존을 연장하지 않는다.

사용자 proposal 반영의 binding은 공용 snapshot의 `bodyDeadline`, recovery 원본 기한
(없으면 recovery의 `bodyDeadline`), recovery TTL, 완료 세션 TTL의 최솟값을 쓴다.
`bodyDeadline`은 library의 보존 판정이며 신규 분석 publisher의 위 TTL 계산과 동일한
표현이라고 가정하지 않는다. consumer는 binding TTL 외에도 실제 공용 snapshot과 원본의
유효성을 별도로 확인한다.

## 실제 원문과 게시 완료

S3 사본은 승인 바이트 지문과 실행/회고 지문을 별도로 대조한다. 정규 JSON 내용 지문은
키 정렬·공백 제거·UTF-8의 고정된 JSON 표현을 사용하고 비유한 수를 허용하지 않는다.
바이트 지문과 내용 지문은 용도가 달라 서로 대체하지 않는다. reader는 중복 JSON 키,
누락·만료 원본을 거부한다.

후속 반영은 기존 library의 `retrospective:{execution_id}` 준비 결과·현재 head CAS·
벡터 게시·개정 확정 경로를 사용한다. 최종 트랜잭션은 child의 lease/원본 기한/지문,
원 `EXEC`의 해결 상태와 불변 연결 필드, publisher binding 전체와 현재 공용 기준을
검사한다. 게시 준비 결과의 `followup.completed_playbook_id`는 현재 완료 분석의
산출물 식별자이며, 사용자 반영 대상인 공용 플레이북 식별자와 다를 수 있다.
이 값을 통해 완료 분석의 출처를 검증하고 공용 대상은 binding으로 별도 확인한다.
성공한 child에는 `published_revision`과 `public_playbook_id`를 남기고 lease와
대기 참조를 제거한다. 일부 저장만 성공했으면 UI에서 게시 완료로 표시하지 않는다.

일반 지식 교정은 기존 필드를 보존하는 병합을 사용한다. 관련 실행 절차가 달라졌으면
초안이고 새 실행 승인이 필요하다. 원본 60일·경량 상태 90일은 기존 정책이며, 재개가
이 수명을 새로 시작하거나 사본/요약으로 만료 원본을 부활시키지 않는다.

## 구현 위치와 검증 경계

- Agent/Headless `services/recovery_publication.py`: 공용 게시자의 불변 연결.
- Headless `services/deferred_publication.py`: 보존 검토 등록·발견·재개·lease.
- Headless `adapters/secondary/playbook/library.py`: 공용 기준과 최종 CAS.
- Dashboard `server/utils/retrospectivePublication.ts`: 후속 결과의 제한된 조회 표현.

이 문서는 구현 완료 판정이 아니다. 동결 코드의 최소 원본 만료와 최종 guard는
`test_earliest_original_source_expiry_fences_vector_completion` 및
`test_binding_expiry_is_checked_at_final_cas_even_without_content_change`가 다룬다.
모델 평가 승인이나 배포 환경 성공을 이 스키마 문서로 대신하지 않는다.

## Related ADRs

- [회고 검토와 공용 반영](../adr/agent/0018-playbook-retrospective.md)
- [플레이북 지식과 사고별 런북](../adr/agent/0008-playbook-generation.md)
- [DynamoDB 상태와 조건부 반영](../adr/infra/0005-execution-trace-dynamodb.md)
- [원본·상태 보존](../adr/infra/0002-evidence-storage.md)
