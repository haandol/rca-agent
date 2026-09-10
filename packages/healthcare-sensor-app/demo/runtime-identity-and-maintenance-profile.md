# 런타임 식별 로그와 정비 작업 프로필 후보

기존 infra ADR 0004의 소스 식별·개인정보 경계와 0007의 관측 계약을 구현하는
로그 인터페이스다. 새 IAM 권한이나 분석 도구를 요구하지 않는다.

## 실제 런타임 식별자

서비스 lifespan 시작과 `python -m test_service.maintenance` 시작에서 각각 한 번,
`ECS_CONTAINER_METADATA_URI_V4`의 `/task`를 조회하고 다음 이벤트를 남긴다.
다음 값은 형식 예시이며 관측 결과가 아니다.

```json
{
  "event": "ecs_runtime_identity",
  "status": "available",
  "TaskARN": "arn:aws:ecs:us-east-1:123456789012:task/Healthcare/0123456789abcdef0123456789abcdef",
  "Cluster": "Healthcare",
  "Family": "Healthcare",
  "Revision": "17"
}
```

필드 이름과 문자열 값은 실제 응답 그대로 보존한다. `Cluster`는 짧은 이름 또는
ARN(리소스 고유 식별자)일 수 있다. ARN이나 소스 리비전을 다른 필드로 조립하지
않는다. 잘못된 타입이나 형식은 생략한다. 일부 필드만 있으면 `status=partial`,
`reason=incomplete_metadata`, `missing_fields`와 유효한 필드만 기록한다.
모두 없으면 `status=unavailable`이다.

환경변수가 없으면 `status=unavailable`, `reason=not_ecs`를 기록한다.
허용하지 않은 URL은 `reason=invalid_endpoint`, `error_type=ValueError`다.
시간 초과·전송 오류·잘못된 응답은 `reason=metadata_unavailable`과 예외 클래스
이름만 기록한다. 실패 본문·URL·예외 메시지·환경변수 목록은 기록하지 않으며 서비스와
정비 작업은 계속 시작한다. 애플리케이션 종료 취소는 삼키지 않는다.

- `http://169.254.170.2/v4/<id>`만 허용하고 `/task`만 요청한다.
- 연결·헤더·응답 본문을 합쳐 최대 2초를 허용하며 재시도하지 않는다.
- 숫자 IP로 직접 연결한다. DNS·프록시·리다이렉트·임의 포트를 사용하지 않는다.
- 자격 증명 경로, `/taskWithTags`, 태그 API에 접근하지 않는다.
- 헤더는 8 KiB, 본문은 256 KiB로 제한한다. HTTP/1.0의 연결 종료 방식으로 읽으며
  지원하지 않는 전송/압축 형식은 안전한 부재로 처리한다.
- `Containers`, 환경변수, 라벨, 네트워크, 태그 등 다른 응답 필드는 버린다.
- 요청 처리 중에는 다시 조회하지 않는다.

정비 CLI는 런타임 조회 전에 기존 `source_manifest()` 결과를
`event=source_manifest`로 기록한다. 설치된 실제 Python 파일 해시와 `fingerprint`,
`revision`, `verified`이며 추정한 소스 식별자가 아니다. 빌드 manifest가 없는 로컬
소스는 기존대로 `verified=false`다. 빌드된 소스가 manifest와 다르면 기존 서비스
정책과 동일하게 시작을 거부하고 락을 잡지 않는다. 서비스의 기존 DB 어댑터
source_manifest 이벤트는 그대로 유지한다.

## 분석·실행 담당자에게 전달할 인터페이스

CloudWatch에서 `event=ecs_runtime_identity`를 검색하고 `@logStream`을 보존한다.
정비 `maintenance_lock_acquired` 이벤트와 같은 스트림의 식별 이벤트를 연결하면 실제
`TaskARN`을 읽을 수 있다. `backend_pid`, `run_id`, `db_wait_snapshot`은 기존
이벤트에서 계속 읽는다. ARN을 얻었다는 사실만으로 복구 소유권이 승인되지는 않는다.
실행 전에 실제 태스크 상태·소유 태그·startedBy를 검증하는 기존 승인 절차가 필요하다.

두 증상 알람 Description의 기존 증상 설명 뒤에 다음 중립적 좌표가 붙는다.
`ns`는 실제 배포 네임스페이스다.

```text
Resources: LogGroup=/ecs/<ns>/healthcare; ECSCluster=<ns>Healthcare; ECSService=<ns>Healthcare; RDSInstance=<lowercase-ns>-postgres.
```

원인·정비 작업·정답·실행 지시는 포함하지 않는다. 소스 저장소는 현재 스택에 확정된
좌표가 없으므로 추정해서 넣지 않는다. 메트릭 차원은 기존
`Healthcare/Sensor`, `ServiceName=healthcare-sensor-app` 그대로다.
이 변경은 분석 DTO가 Description을 전달하도록 바꾸지 않는다. 메인 담당자는 기존
CloudWatch 알람 메타데이터 조회 경로 또는 별도 승인된 DTO 변경으로 이를 전달한다.

## 정비 프로필 후보

[복사용 템플릿](maintenance-profile.toml.example)은 자동 적용하지 않는다.
`packages/infra/config/loader.ts`가 이미 지원하는 세 설정만 사용한다.

| 설정 | 후보 |
|---|---|
| `DB_STATEMENT_TIMEOUT_MS` | `2000` |
| `DB_OBSERVABILITY_ENABLED` | `true` |
| `DB_OBSERVABILITY_INTERVAL_SECONDS` | `1` |

정상 측정 전에 설정하고 정상·정비 락 유지·복원 구간 모두 같은 값을 사용한다.
기본 요청 슬롯 간격 5초, 동시성 1, 조회 limit 20, 풀 5+10, 풀 획득 제한 30초,
지표 집계 30초와 기존 장애 플래그 기본값은 바꾸지 않는다. 이미지와 서비스 태스크를
정비 시작·해제 때 교체하지 않는다.

2초 문장 제한은 락에 막힌 저장을 실제 실패로 완료시켜 `VitalIngestFailures`에
반영하기 위한 후보다. 1초 관측 간격은 그 대기 구간의 실제 차단 관계를 포착하기 위한
후보다. 관측 실행 시간과 스케줄링 지연이 있으므로 성공 보장값은 아니다.
요청량을 늘리거나 실패 구간에서만 제한 시간을 바꾸지 않는다.

실제 준비 완료는 별도로 확인한다.

1. 정상·복원에서 연속된 완전한 1분 구간 두 개에 저장 시도 >0, 저장 실패 0,
   환자 조회 샘플 >0와 기존 조회 임계치 미만을 확인한다.
2. 정비 중 실제 차단 PID·대기 락·정비 작업을 연결하고 저장 실패가 알람의 1분/2회
   평가 기준을 만족하는지 관측한다. 요청 시작량과 skipped도 비교한다.
3. 승인된 소유 태스크 중지가 실제 롤백·락 해제·저장 재개보다 앞섰음을 확인한다.
   자연 만료나 운영자 cleanup은 승인 복구 성공이 아니다.
4. 실제 알람 전달, 한 승자 분석, 승인·실행·회고, 소유 리소스 정리를 각각 확인한다.
   로컬 테스트나 제공 관측 모델 평가는 이 배포 검증을 대신하지 않는다.

현재 템플릿은 보정 후보이며 AWS 신뢰성·리허설 소요시간을 측정한 결과가 아니다.
전역 기본값이나 `packages/infra/.toml`을 자동으로 바꾸지 않는다.

## 확인한 AWS 1차 문서

2026-09-10에 AWS 공식 문서의 응답 필드와 예시를 확인했다.

- [Fargate v4 엔드포인트](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-metadata-endpoint-v4-fargate.html)
- [응답 필드: Cluster, TaskARN, Family, Revision](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-metadata-endpoint-v4-fargate-response.html)
- [형식 예시: Revision은 문자열](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-metadata-endpoint-v4-fargate-examples.html)
