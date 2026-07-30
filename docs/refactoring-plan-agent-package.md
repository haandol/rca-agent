# agent 패키지 리팩토링 기록

> 이 문서는 **완료된** 리팩토링의 이력이다. 현재 구조는 Hexagonal Architecture 결정을 다루는 ADR과 실제 코드를 참조하라 — 아래 줄 수·파일 목록은 작업 당시의 값이며 갱신하지 않는다.

## 배경

`main.py`(914줄)에 SQS 진입점·파이프라인·하위호환 re-export가 뒤섞여 있었고, 동일한 9-stage 파이프라인이 `main.py`와 `services/pipeline.py` 양쪽에 이중 구현되어 있었다. 실제 비즈니스 로직은 패키지 루트에 있고 `services/*`는 이를 re-export하는 래퍼여서 코드 위치가 역전된 상태였다.

## 완료된 단계

| Phase | 내용 |
|-------|------|
| 1 | `main.py` 914줄 → 순수 진입점으로 축소. 레거시 `_Agents`·`_process_alarm`·`_run_pipeline` 삭제, `PipelineOrchestrator`를 유일한 파이프라인 진입점으로 단일화. SQS 폴링 루프를 adapter로 이동 |
| 2 | 루트의 `scoping.py`·`evidence.py`·`report.py`·`playbook_gen.py`·`notification.py` 실제 구현을 `services/*`로 이동. 테스트 import와 `@patch` 경로를 `rca_agent.services.*`로 일괄 변경 |
| 3 | `trace_store.py` → `adapters/secondary/trace/`, `session_store.py` → `adapters/secondary/session/`로 이동 |
| 4 | `_run_pipeline`(520줄 단일 메서드)을 스코핑·가설생성·검증루프·가설확정·보고서/알림 5개 메서드로 분할 |
| 5 | `prompts.py`(409줄)를 `prompts/` 패키지로 분리 — 공통 지시문 + 스테이지별 모듈 |
| 후속 | Phase 2~3에서 남은 루트 re-export 스텁 전량 제거. 벡터 검색을 어댑터(Port) 경유로 단일화하여 서비스 레이어의 중복 검색 구현과 cosine 거리 오해석 결함을 함께 해소 |
