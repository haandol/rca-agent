# 현실적 데모 구현 검토 기준

사용자는 네 가지 실제 장애 시나리오 제안을 적용하도록 승인했다. 정상/결함 소스 이미지, 설정 변경, 소유 정비 트랜잭션, 같은 부하의 정상·결함·복원 비교와 원본 관측을 구현한다. 기존 RCA 효율 수정과 오래된 모델 실패는 별도 변경 이력이며 손대지 않는다. 모델/평가 임계치·기각 정책 변경은 승인하지 않았다.

## Context
- intent: 실제 설정·SQL·연결 수명에서 발생한 증상으로 네 원인을 구별하고 복원 결과를 확인한다.
- preconditions: 사설 서비스와 소유 실행 ID, 정상 이미지, 같은 요청 조건, 시간과 출처가 보존된 실제 관측이 필요하다.
- contracts: 각 ADR 전체 Decision 및 Requirement contract, 기존 허용 원인 집합·안전성·승인 경계를 유지한다.
- scopeAndRisk: DB 수명·동시성·영속 복원 기록·외부 제어·입력 무결성을 포함하므로 full review. AWS 배포 E2E는 미실행이며 로컬 실측이나 mock을 AWS 성공으로 해석하지 않는다.

## 검토 경로
- H1: 동일한 입력에서 실제 결함을 재현한다. 부하·서비스·실제 PostgreSQL·불변 소스를 따라가며 정상/결함/복원 차이를 확인한다. 구조도와 요청 순서도가 필요하다.
- H2: 실행 중 실패해도 소유 변경을 복원한다. CLI journal→ECS/CloudWatch 조회·변경→복원 확인을 따라간다. 실패/복원 분기 순서도가 필요하다.
- H3: 사고 시점의 관측으로 두 엔진을 같은 기준에서 평가한다. 출처/시간 검증→중립 관측→기존 파이프라인→정규화 결과 경계를 확인한다. 데이터 흐름도가 필요하다.

세 ADR의 정확한 D0/Rn 목록은 contract-ledger.json. 각 ADR 별 해당 행을 위 수직 기능에 한 번씩 배정하여 결과를 기록한다. 구현 범위는 diff가 상한이 아니며 원래 호출 경로/레거시 호환 코드도 추적한다. tests/scenarios 데이터는 작업자 D가 최종 동결 중이므로 최종 지문을 확인한다.

## 검증
`pnpm run test:unit` 통과: agent722/headless799/healthcare117/infra44, healthcare2skip/1xfail. CLI node harness3pass. 전체 verify는 작업 중 새 JS formatting에서 멈췄으며 최종 동결 후 재실행한다. Local PG 증거 /private/tmp/rca-realism-ea868dcd; 최종 증거 경로는 추가 통보한다.

## 외부 규칙과 한계
저장소 docs/adr/concepts.md 부재 및 기존 seeded rule 문서 stamp/driver/번호 경고는 알려진 legacy 경고다. 저장소 규칙을 읽고 필요한 경우 /private/tmp/rca-adr-tools-0.8.19/templates/adr를 참고한다. 브라우저 file URL 자동 승인이 거부됐으므로 HTTP 또는 shell opener 우회 금지; 보고서는 정적 검증과 파일 전달만 가능하다.
