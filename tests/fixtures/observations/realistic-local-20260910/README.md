# 운영자 전용: 로컬 PostgreSQL 원본과 입력 경계

현재 카탈로그 원본은 `proof-review-fixed-01.json.txt`다. HTTP 오류 로그 보호를 수정한
현재 소스로 실제 PostgreSQL 재현을 실행한 원본을 바이트 그대로 복사했다.
실행 ID `local_c1cf824ddbfd4bdb`, SHA-256 `83aac573c02bd6eb9596ced5e0797460018704b62b955905b68b15707d1b4f2b`.
원본 checks는 네 메커니즘, 취소·자원 정리, UTC 단계·요청 구간, 공통 소스 기반을
확인한다. **이 파일은 복원 결과·운영자 라벨을 포함하므로 모델에 전달하지 않는다.**
`.json.txt`는 formatter가 원본 바이트를 바꾸지 않게 하는 보존 형식이다.

## 모델에 전달하는 내용

`tests/scenarios/*.json`의 observations는 `scenario-capture-boundary.mjs`가
정상 기준과 사고 구간에서 선택한 관측이다. `source.captureId`는 중립 참조이며
원본 경로·case/phase 라벨·정답·복원 성공 값을 포함하지 않는다.
별도 `operator-capture-map.json`이 capture ID를 원본의 숫자 JSON 포인터에 연결한다.
`source-snippets.json`은 측정 소스 해시와 일치하는 실행 코드의 검토용 스냅샷이다.
두 어댑터는 provenance, expectation, operator mapping을 모델에 전달하지 않는다.

- 모델 입력은 baseline + incident만 가진다. restore 단계는 제외한다.
- 락의 fault 단계 안에 있는 restored_write, release_events, bounded_hold_events도 제외한다.
- 락 cutoff는 blocked_write.completed_at이며 실제 released_at보다 이르다.
- 다른 사례는 원본 measurement_completed_at까지의 관측만 선택한다.
  dispose 전·후 정리 카운터와 cleanup checks는 전달하지 않는다.
- UTC 시각을 새로 생성하거나 파일 시각으로 대체하지 않는다. 필요한 시각이 없으면
  projection은 실패한다. snapshot은 호출된 측정 구간 안의 원본이며 별도의 수집 시각을 추정하지 않는다.
- 코드·설정·SQL 횟수·연결 반환·차단 관계는 유지하고, pool obs-06은 사고 중 DB 상한·대기
  snapshot, lock obs-04는 해제 전 SQL 실행·획득 시간으로 필수 증거를 구성한다.
  필수 증거 개수, 원인 확정, 경쟁 원인별 증거, 실행·안전성 조건은 낮추지 않았다.

## 운영자용 정상→결함→복원 요약

아래는 재현 결과 설명용 집계다. 복원 열은 모델 입력이 아니다. 개별 요청 지연의
중앙값을 CloudWatch 60초 Average나 AWS 알람 판정값으로 해석하지 않는다.

| 사례 | 원본 구간 | 실측 결과 |
|---|---|---|
| query | normal | 행 120; SQL/조회 1; 지연 중앙값 5.018ms |
| query | fault | 행 120; SQL/조회 121; 지연 중앙값 50.758ms |
| query | restore | 행 120; SQL/조회 1; 지연 중앙값 10.937ms |
| pool | normal | 풀 8; 요청 8; 성공 8; TimeoutError 0 |
| pool | fault | 풀 1; 요청 8; 성공 1; TimeoutError 7 |
| pool | restore | 풀 8; 요청 8; 성공 8; TimeoutError 0 |
| lock | normal | 쓰기 ok |
| lock | fault | 차단 쓰기 error, 519.778ms; 해제 후 쓰기 ok |
| lock | restore | 쓰기 ok |
| exception | normal | 예외 후 체크아웃 [0, 0, 0]; 후속 쓰기 ok |
| exception | fault | 예외 후 체크아웃 [1, 2, 3]; 후속 쓰기 error |
| exception | restore | 예외 후 체크아웃 [0, 0, 0]; 후속 쓰기 ok |

## 보존 이력과 남은 검증

`proof-time-aligned-01.json.txt`와 `proof-time-aligned-03.json.txt`는 이전 UTC 입력이며 그대로 보존했다.
`proof-01.json.txt`는 checks가 없는 실패/미완료 실행(조회 오류 포함),
`proof-02.json.txt`는 UTC 구간 계측 전 통과 실행이다. 둘 다 원본 그대로 보존했다.
과거 수치나 성공 기록을 새 회차로 재사용하지 않는다. 초기 합성 예시는
`tests/fixtures/historical/illustrative-drafts/`, 원래 네 모델 입력과 정규화 fixture는
`tests/fixtures/historical/original-four/`에 보존했다.

현재 데이터는 고정 횟수의 직접 서비스 호출이며 AWS 알람·HTTP 지속 부하·30초 지표
방출·60초×2 평가·실제 증거 탐색 성공이 아니다. 조회 배포 기본 임계치 500ms는
별도 보정 대상이며 카탈로그 threshold는 생략한다. 원본에 없는 AWS 리전·RDS
식별자·SQL 원문은 생성하지 않는다. SQL 해시와 알고리즘 표시는 원본대로 보존한다.
네 입력은 model-eval만 선언한다. 진단 모델 실행에서 나온 실패 결과는 `tests/results/model/`에 보존하며, 새 기준선은 승인하지 않았다.

최종 원본의 `identical_query_inputs`와 `local_query_threshold_relation`도 통과했다.
조회 입력 필터·행 제한은 baseline/incident 관측에 보존한다. 복원 구간을 사용한
`local_query_calibration`은 운영자 전용으로 남기며 모델에 전달하거나 AWS threshold로 채택하지 않는다.
