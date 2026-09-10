# 현실적 시나리오: main / DB 측정 인계

2026-09-10. 카탈로그 작업은 agent0016·infra0007의 승인된 Proposed 계약 범위다.
현재 카탈로그에는 main/DB 작업자의 실제 로컬 PostgreSQL `proof-time-aligned-03` UTC 출력을 반영했다.
최종 원본·이전 `proof-02`와 실패한 `proof-01`은 `tests/fixtures/observations/realistic-local-20260910/`에
바이트 그대로 보존했다. 측정 작업을 이 카탈로그 작업에서 재실행하지 않았다.
[실측 요약과 한계](../../tests/fixtures/observations/realistic-local-20260910/README.md)를 참고한다.

알람 외곽은 여전히 합성 예시다. AWS 알람 시각·리전·실측 임계치를 만들지 않는다.
B가 기록한 단계·요청의 UTC 구간을 사용한다. 필요한 시각이 없으면 projection은 실패한다.
SQL 이벤트의 hash·횟수·시간은 원본 값이며 원문을 역으로 추정하지 않는다.
초기 숫자·시각 예시는 `tests/fixtures/historical/illustrative-drafts/`에 보존했다.
모델 결과 fixture와 기준선은 만들거나 승인하지 않는다.

조회·세션 정리의 코드 관측은 `revision/query.py`, `revision/session.py`와
r2/r3 overlay의 실행 줄·SHA-256이며 실제 측정 source manifest와 일치함을 확인했다.
주석·docstring의 원인 설명은 제외했다. SOURCE_REVISION=r1/r2/r3는 빌드 입력이다.
[브라우저용 설명](realistic-scenarios.html)에서 검증 경계를 볼 수 있다.

| 카탈로그 ID | 원인 유형 | 정상→결함→복원에 필요한 증거 |
|---|---|---|
| `pool-config-regression` | `unsupported` | 유효 풀 설정과 변경 이력, 풀 획득 대기, 반환·요청 종료 뒤 연결, DB 전체 연결 상한, 같은 요청 시작 계획·실제 시작·완료·미시작 수 |
| `query-amplification` | `slow-query` | 같은 결과 행 수·내용, SQL 패턴별 횟수·개별 시간·전체 조회 시간, 실제 일괄/행별 조회 코드 차이와 이미지·소스 식별자, DB 대기 표본 |
| `maintenance-transaction-lock` | `unsupported` | 소유 실행 ID·정비 작업·연결 식별자, 트랜잭션 시작/종료 시각, 차단자/대기자 관계·락 모드·SQL, 저장 실패·복원, 세션 반환·조회 횟수 |
| `exception-session-cleanup` | `db-leak` | 같은 예외 입력, 요청별 획득/반환·요청 종료·이후 정상 저장, 실제 예외 정리 경로 diff·이미지·소스 식별자, 유효 풀 설정·DB 대기 |

카탈로그 위치: `tests/scenarios/<id>.json`. 각 관측의 `summary`는 원시 레코드
JSON 문자열이다. 두 어댑터가 실제로 모델에게 보내는 이 필드 안에 시간 구간,
단위, 리소스, 출처와 누락 표시를 모두 보존한다. 중립 ID `obs-01`…`obs-06`은
시나리오 내부 참조이며 정답 유형을 담지 않는다.

| 관측 ID | 풀 설정 | 조회 증폭 | 정비 락 | 예외 정리 |
|---|---|---|---|---|
| `obs-01` | 저장 증상 | 조회 시간·결과·SQL 횟수 | 저장 증상 | 예외 이후 저장 증상 |
| `obs-02` | 설정 diff | 조회 코드 diff | 차단 관계·SQL·소유권 | 요청별 연결 수명 |
| `obs-03` | 연결 반환 | SQL 개별 시간 | 설정·이미지·작업 변경 이력 | 예외 정리 코드 diff |
| `obs-04` | SQL 횟수·실행 시간 | 측정 소스 변경 이력 | 해제 전 SQL·획득 시간 | 측정 소스·풀 설정 |
| `obs-05` | 8개 요청·1000행 배치 | 동일 풀 설정·획득 시간 | 연결 수명 | 동일 풀 설정·요청별 획득 |
| `obs-06` | 사고 중 DB 상한·대기 snapshot | 요청별 반환 | 동일 풀 설정·획득 시간 | 사고 중 차단 관계 표본 |

실제 증거를 보내는 측은 원본 출력 경로, 실행 ID, UTC 구간, 단위, 리소스 좌표,
소스 리비전/이미지, 부하 설정, 정상·결함·복원의 성공 여부를 함께 남긴다.
SQL 바인드 값·환자 데이터·자격 증명은 포함하지 않는다.
원본이 없는 지표는 보강 대기 항목으로 남기고 반증으로 요구하지 않는다.
교체 후 각 경쟁 원인의 필수 증거 집합은 서로 겹치지 않게 유지한다.

배포 제어 구현 여부는 infra 작업자/main이 확인한다. 네 입력은 현재
`model-eval`만 선언한다. 실행 가능한 주입·소유권 확인·원래 상태 보존·중단 시
정리·증상 회복 확인이 연결되기 전 `deployed-e2e`를 추가하지 않는다.
실행 경로가 생겨도 배포 E2E 성공을 의미하지 않는다.

작업공간의 `scripts/run_realistic_demo.py`와 연결할 이름은 아래와 같다.
이는 인터페이스 대응표이며 배포 실행 결과가 아니다. 호출 전 main이 현재
제어·복원 계약과 이미지 식별자를 검증한다.

| 카탈로그 ID | 제어의 `--scenario` | 빌드 소스 |
|---|---|---|
| `pool-config-regression` | `pool-config` | r1, 풀 설정 변경 |
| `query-amplification` | `query-revision` | r1 → r2 → 원래 리비전 |
| `maintenance-transaction-lock` | `maintenance-lock` | 원래 이미지의 소유 정비 작업 |
| `exception-session-cleanup` | `session-revision` | r1 → r3 → 원래 리비전 |

## 복구 검토 조건

모든 사례는 확정 원인, scoping/hypotheses/validation/report/playbook,
실행 절차와 DRAFT 상태, 대상 사전 확인, 사용자 승인, 롤백 조건, 사후 검증을
요구한다. 평가가 실제 복구를 실행하지 않는다.

- 풀: 원래 실행 정의와 설정의 부재까지 복원하고 같은 부하에서 저장·대기를 확인한다.
- 조회: 원래 이미지로 복원하고 같은 응답 내용·SQL 횟수·조회 시간을 확인한다.
- 락: 실행 ID로 소유권을 확인한 정비 작업의 트랜잭션만 롤백하고 저장 재개를 확인한다.
- 정리: 원래 이미지 복귀와 결함 태스크 종료 후 같은 예외 입력의 반환 및 정상 저장을 확인한다.

레거시 fault reset 성공만으로 위 복원이 성립하지 않는다. 파괴적 작업 금지와
기존 승인 경계는 유지한다. 평가기는 기존 구조·안전성 계약을 그대로 검사한다.
실제 설정·이미지·작업과 절차의 정합성은 산출물의 사람 검토에도 포함한다.

## 모델 입력의 시간·정보 경계

모델 입력은 중립 capture ID와 baseline/incident만 포함한다. 원본 파일 경로,
case=lock 같은 선택자, fault/restore 라벨, 원인 정답은 전달하지 않는다.
운영자 전용 operator-capture-map.json이 capture ID를 숫자 JSON 포인터에 연결한다.
전체 proof의 복원 결과는 재현·평가 증거로 보존하고 두 어댑터의 프롬프트에 넣지 않는다.

락 분석 마감은 실제 차단 쓰기 종료 `2026-09-10T02:27:04.409843+00:00`이다.
정비 해제 `2026-09-10T02:27:04.420616+00:00` 뒤의 값은 포함하지 않는다.
다른 입력은 measurement_completed_at까지 선택하며 dispose 전/후 정리 카운터도
전달하지 않는다. 요청별 획득·반환과 예외 이후 체크아웃은 사고 중 원본 관측으로 유지한다.

복원 전 증거로도 필수 원인을 판별하도록 pool obs-06은 실제 DB 상한·대기 snapshot,
lock obs-04는 차단된 SQL 횟수·시간·획득을 요구한다. 원인 유형·필수 ID 집합·독립
경쟁 원인·확정·필수 산출물·복구 안전성 조건은 그대로다. 정답을 복원 결과에서
얻거나 기존 기대 조건을 약하게 만들어 통과시키지 않는다.

공용 조회 메트릭·알람은 PatientVitalsQueryDuration, Healthcare/Sensor,
ServiceName healthcare-sensor-app, Milliseconds, Average, 60초×2,
${ns}-Healthcare-PatientVitalsQueryLatency다. 기본 500ms는 AWS 알람을 검증한
임계치가 아니므로 별도 지속 부하 보정이 필요하다. 운영자용 수치·원본 해시는
[실측 요약](../../tests/fixtures/observations/realistic-local-20260910/README.md)에 있다.

## 카탈로그·어댑터 작업 검증

- Strands 관련 83개, Headless Codex 관련 63개 테스트 통과.
- 루트 계약 135개 중 134개 통과. 유일한 실패는 기존 기준선 digest/file-set 불일치다.
- 전체 root 포맷 검사, 변경 Python Ruff, 변경 공백 검사, HTML/SVG/링크 검사 통과.
- 복원·정리 결과와 운영자 라벨을 바꾸어도 모델 관측이 동일함을 검사했다.
- 날짜 누락 시 projection 실패, 락 마감이 실제 해제보다 이른지, 실제 양쪽 엔진 입력에
  원본 경로·operator map·복원 결과가 없는지 검사했다.
- canonical model fixture는 0개다. baseline·evaluator 정책은 수정하지 않았으며,
  라이브 모델/AWS 실행·커밋·ADR 수정은 하지 않았다.
- 체크포인트 전체 평가 입력 digest: `sha256:2e55a2d8af232a5ac1f19be705f673182c7daf56fad06cd6466025532065fb9a` (69개 파일).
  [기계 판독 체크포인트](catalog-validation-checkpoint.json)는 기준선 승인이 아니다.
  통합 중 계약 파일이 바뀌면 live 평가 전에 digest를 다시 비교한다.

최종 채택 자료는 B의 proof-time-aligned-03이다. 이전 01 기반 digest는 이 체크포인트로 대체한다.
동일 조회 입력과 로컬 임계 관계도 원본에서 검증됐지만, 복원을 사용한 로컬 보정 결과는 모델 입력과 AWS 임계치에서 제외했다.
