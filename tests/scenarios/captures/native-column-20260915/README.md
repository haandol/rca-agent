# 실제 로컬 PostgreSQL 관측

`evidence.json.txt`는 헬스케어 작업이 제공한
`packages/healthcare-sensor-app/demo/proofs/20260915-final-observer/evidence.json`의
바이트 그대로 복사한 원본이다. SHA-256은
`66e2622416dae216eb21f1d94fa68b893d315c33c9e37e795fbf74d332872f2b`다.
새 DB 실행을 이 폴더에서 수행하거나 과거 AWS 관측을 변환하지 않았다.

원본 실행 ID는 `local_be25e73e1b254178`이다. 정상 구간은 6행을 커밋했고 장애
구간은 실제 PostgreSQL `42703`과 0행 추가를 기록했다. 두 구간의 실제 테이블
컬럼 목록은 같고 `timestamp`가 있으며 `sampled_at`은 없다. 조회와 헬스도 정상이다.

`normal-write.py`는 제공된 원본의 정상 소스 지문과 일치하는 소스 바이트다.
`fault-write.py`는 동일 바이트에서 빌더와 같은 한 컬럼 상수 치환을 적용했고,
그 결과의 SHA-256이 원본 장애 소스 지문과 일치하는 것을 확인했다.

| 소스 | 원본에서 관측된 SHA-256 |
|---|---|
| 정상 `revision/write.py` | `7ec7698e9d1f8cf97a86e2d0d56894f6ed7d6f80c033fca5e73ead456b75ea7f` |
| 장애 `revision/write.py` | `7780ff5c28898bf605d0a5f9026439d34cc6f3feaa38f0ca3cfd6105cf6527de` |

`projectWriteColumnCaptures`가 정상·사고 구간만 활성 fixture로 추출한다.
사고 입력의 종료 시각은 `2026-09-15T14:57:29.459021+00:00`이다. 복원 구간과
운영자 검사 결과는 원본에 보존하되 모델 입력에서 제외한다. 대응 위치는
`operator-capture-map.json`에 기록한다.

알람 외곽 입력은 합성 예시다. 로컬 EMF 출력은 CloudWatch에서 조회한 지표 구간이
아니며, 이 원본에는 AWS 이미지 지문·태스크 정의·알람 전이·RCA 결과·승인 실행
성공이 없다. 초기 합성 계약 예시는 `../../history/synthetic/`에 보존한다.
