# ADR 0005: 코드 변경 분석 — 배포 코드 diff의 LLM 기반 결함 탐지

Date: 2026-04-21

## Status

Proposed

## Context

배포 이력(F8)에서 의심 배포가 식별된 후, 해당 배포의 코드 변경 내역(diff)을 분석하여 장애를 유발할 수 있는 결함 패턴을 자동으로 탐지해야 한다. Should-Have 기능으로, 코드 저장소 접근이 불가해도 다른 증거에 의존하여 RCA를 진행할 수 있다.

## Decision

**CodeCommit/GitHub API 기반 MCP 도구 + LLM 결함 패턴 분석** 전략을 채택한다.

### 핵심 결정사항

1. **MCP 도구 인터페이스**: `get_code_diff(repo, commit_id_before, commit_id_after)` 형태의 도구를 제공한다. CodeCommit GetDifferences / GitHub Compare API를 래핑한다.

2. **LLM 분석**: diff를 Bedrock에 전달하여 "이 코드 변경에서 장애를 유발할 수 있는 패턴이 있는가?"를 판단한다.

3. **탐지 대상 패턴**: 리소스 미반환(커넥션/파일/스레드), 예외 처리 누락, 설정값 변경, 타임아웃 변경, 쿼리 변경 등을 탐지한다.

4. **크기 제한**: diff가 LLM 컨텍스트 윈도우를 초과할 경우 변경된 파일을 우선순위로 분할 분석한다. 수천 줄 이상의 diff는 변경 파일 목록만 요약하고 핵심 파일만 상세 분석한다.

5. **MVP 지원 범위**: CodeCommit과 GitHub를 지원한다. 그 외 시스템은 MVP에서 미지원으로 표시한다.

## Consequences

### Positive

- LLM의 코드 이해 능력으로 수동 코드 리뷰 없이 결함 패턴 자동 탐지
- 배포 이력과 연계하여 장애 원인을 코드 수준까지 특정 가능
- 보고서에 구체적인 코드 결함 증거를 포함하여 영구 조치 방안 제시 가능

### Negative

- LLM의 코드 분석 정확도가 코드 복잡도와 언어에 따라 달라질 수 있음
- 코드 저장소 접근 권한 설정이 별도로 필요

### Risks

- 코드 저장소 접근 권한 부재 시 "수집 불가"로 표시하고 다른 증거에 의존한다. Should-Have 기능이므로 RCA 전체 흐름에 영향을 주지 않는다.
- 대규모 diff에서 LLM이 핵심 결함을 놓칠 수 있다. 분할 분석과 결함 패턴 키워드 사전 필터링으로 완화한다.

## Related

- [ADR tools/0004: 배포 이력 조회](0004-deploy-history.md) — 의심 배포를 식별하는 이전 단계
