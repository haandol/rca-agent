## 핵심 원칙

- **전문 역할 사용**: 메인은 Agent tool 호출과 결과 전달만 수행한다.
- **증거 기반 RCA**: RCA 결과는 읽기 전용 MCP 증거에 근거한다.
- **서버 측 복구 게이트**: 모델의 confirmed 주장을 신뢰하지 않는다. narrow MCP가
  최신 validation 산출물을 직접 재검증한다.
- **Fail-closed**: 미확정, unsupported, ambiguous 원인이나 서로 다른 allowlisted 원인을
  가리키는 미해소 경쟁 가설이 있으면 어떤 변경도 실행하지 않는다.
- **제한된 변경**: Healthcare 네 reset 외 HTTP, Bash, ECS update를 실행하지 않는다.
- **보고 연속성**: 복구 미실행·차단·실패에도 Report를 실행한다.
- **실행 격리**: 현재 실행 정보만 사용하고 이전 실행을 재사용하지 않는다.
- **증거 시간 구분**: current alarm window와 historical comparison window를 시각으로 구분한다.
- **과거 로그 격리**: 이번 실행 이전 수동 테스트 로그를 현재 장애 증거로 사용하지 않는다.
- **검증 원본 보존**: 서버가 기록한 `remediation.json.verification`을 수정하지 않는다.
- **한글 작성**: 모든 산출물과 보고서는 한글로 작성한다.
