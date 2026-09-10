# 현실적 데모 검토 보고서

main의 최종 코드 종료 합성을 반영했다. 세 보고서의 전체 증거 판정은 **INCONCLUSIVE**, 독립 코드 재검토는 **PASS**다. 열린 코드 발견사항은 없으며 계약 26행은 21 PROVEN·0 VIOLATED·5 UNVERIFIED다. 실제 runtime·알람과 모델 품질·승인 증거의 한계를 기록하며 AWS를 새 ADR 상태 승격 선행조건으로 추가하지 않는다. 세 ADR은 Proposed로 유지한다.

| 대상 | 계약 행 | 기능별 SVG | 공식 materialize/validate/render | 정적 SVG·앵커 |
| --- | ---: | ---: | --- | --- |
| [infra-0004](infra-0004/adr-impl-review-report.html) | 7 | 2 | PASS | PASS |
| [infra-0007](infra-0007/adr-impl-review-report.html) | 11 | 3 | PASS | PASS |
| [agent-0016](agent-0016/adr-impl-review-report.html) | 8 | 3 | PASS | PASS |

각 디렉터리의 `findings.json`, `implementation-review.md`, `adr-impl-review-report.html`이 현재 산출물이다. `official-validation.json`, `static-validation.json`, `artifact-status.json`, `browser-status.txt`에 검증 기록을 남겼다. `draft-*` 파일은 이전 초안 검사 이력이며 현재 판정을 나타내지 않는다.

현재 판정의 권위는 main의 최종 코드 종료 통보와 Curie의 `sufficiency-residual-final-recheck.md`, `sufficiency-closed-coverage.json`이다. 원본·중간 검토 문서는 이력으로 보존한다. 원래 계약 문구·D0/Rn·전체 ADR 본문을 유지했고 각 행은 해당 보고서에서 한 기능에만 배정했다.

F1/F2/F3/F4, N1과 최소 관측시간 U3는 종료됐다. N2는 선택적 정리로 변경하지 않았다. 최신 단위 테스트는 1,771 PASS(agent740/headless824/healthcare125/infra82), 루트136 PASS·이전 승인 digest 실패1개다. format/lint/build/typecheck는 PASS다. 마지막 F2는 Python56·Node4와 독립 원래 반례를 통과했다.

실제 compiled proof SHA-256은 `83aac573c02bd6eb9596ced5e0797460018704b62b955905b68b15707d1b4f2b`다. 현재 proof와 이미지3개는 verified=true이며 소스가 일치한다. verified=false는 UNBUILT 체크아웃 manifest에만 해당한다.

모델 회차 `realistic-final-20260910T025818Z-a57123`은 6개 완료·2개 Strands 실행 중이다. digest는 `8dc813d320341b5347ed5f299a5c52217a5c84a7d02863e8be79e2672e860cfc`다. 최종 품질 수치와 정리 기록은 main의 마지막 통보 뒤 갱신한다. AWS 배포 E2E는 미실행이며 500ms 알람 성공을 주장하지 않는다.

**NOT_OPENED — 정책 차단.** 이전 자동 승인 검토가 local file:// 열기를 거부했고 사용자가 HTTP·shell·다른 브라우저 우회도 금지했다. open helper를 실행하지 않았다. 브라우저 시각 검사는 하지 않았으며 정적 SVG XML·관계 도형·내부 링크만 검사했다. 외부 렌더링 의존성이나 퀴즈·PR 준비 게이트는 없다.

공식 생성기의 코드 목차 링크와 대상 ID 대소문자 차이는 담당 HTML에만 한정해 정규화했다. 원본 생성 해시와 변경한 ID는 `official-validation.json`에 남겼다. 제품 코드·ADR·기준선·다른 에이전트 소유 파일은 변경하지 않았다.
