# ADR 0005: 코드 변경 분석 — GitHub MCP 서버 기반 배포 코드 diff의 LLM 결함 탐지

Date: 2026-04-22

## Status

Accepted

## Context

배포 이력(tools/0004)에서 의심 배포가 식별된 후, 해당 배포의 코드 변경 내역(diff)을 분석하여 장애를 유발할 수 있는 결함 패턴을 자동으로 탐지해야 한다. GitHub 공식 MCP 서버(`github/github-mcp-server`)가 PR diff, 커밋 diff, 파일 내용 조회를 모두 지원하므로 커스텀 구현 없이 활용할 수 있다.

## Decision

**GitHub 공식 MCP 서버(`github/github-mcp-server`)**를 Strands 에이전트의 MCP 서버로 등록하여 사용한다.

### 핵심 결정사항

1. **MCP 서버**: `github/github-mcp-server`를 Strands 에이전트에 등록한다. Docker 기반 로컬 실행(`ghcr.io/github/github-mcp-server`) 또는 HTTP 원격 연결(`https://api.githubcopilot.com/mcp`) 방식을 선택할 수 있다.

2. **주요 도구**:
   - `get_commit`: 커밋 해시로 diff와 변경 통계를 조회한다. CloudTrail에서 식별된 배포 커밋의 변경 내역을 가져오는 데 사용한다.
   - `list_commits`: 날짜 범위/파일 경로 기반으로 커밋 목록을 조회한다. 의심 배포 전후 커밋을 탐색하는 데 사용한다.
   - `pull_request_read`: PR diff, 변경 파일 목록, 리뷰 코멘트를 조회한다. 배포가 PR 기반일 경우 전체 변경 범위를 파악하는 데 사용한다.
   - `get_file_contents`: 특정 ref의 파일 내용을 조회한다. diff 주변 컨텍스트가 필요할 때 사용한다.

3. **LLM 분석**: 수집된 diff를 Bedrock에 전달하여 장애 유발 가능 패턴을 판단한다. 탐지 대상: 리소스 미반환(커넥션/파일/스레드), 예외 처리 누락, 설정값 변경, 타임아웃 변경, 쿼리 변경 등.

4. **크기 제한**: diff가 LLM 컨텍스트 윈도우를 초과할 경우 변경된 파일을 우선순위로 분할 분석한다.

5. **인증**: `GITHUB_PERSONAL_ACCESS_TOKEN` 환경변수로 PAT를 전달한다. ECS Fargate 환경에서는 Secrets Manager에 저장하고 태스크 정의에서 주입한다.

## Consequences

### Positive

- GitHub 공식 MCP 서버 사용으로 커스텀 코드 유지보수 불필요
- PR diff, 커밋 diff, 파일 내용, 코드 검색 등 풍부한 도구셋 활용 가능
- 배포 이력(CloudTrail)과 연계하여 장애 원인을 코드 수준까지 특정 가능
- LLM의 코드 이해 능력으로 수동 코드 리뷰 없이 결함 패턴 자동 탐지

### Negative

- GitHub PAT 발급 및 권한 관리가 별도로 필요
- 프라이빗 레포지토리 접근 시 PAT에 `repo` 스코프가 필요하여 보안 관리에 주의
- GitHub API 레이트 리밋(5,000 req/hour)에 의해 대규모 분석 시 제약 가능

### Risks

- GitHub 접근 권한 부재 시 "수집 불가"로 표시하고 다른 증거에 의존한다. 코드 분석 없이도 메트릭/로그/배포이력으로 RCA를 진행할 수 있다.

## Related

- [ADR tools/0004: 배포 이력 조회](0004-deploy-history.md) — 의심 배포를 식별하는 이전 단계
