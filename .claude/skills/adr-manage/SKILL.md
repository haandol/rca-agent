---
name: adr-manage
description: ADR 생성 및 수정 시 작성 규칙을 자동 적용한다. 새 ADR 작성, 기존 ADR 수정, ADR 리뷰/검증에 사용한다. "ADR 만들어줘", "ADR 작성", "ADR 추가", "adr create", "새 ADR", "ADR 수정", "ADR 업데이트", "ADR 검토", "ADR 리뷰" 등의 키워드에 트리거. ADR을 직접 편집하거나 ADR 내용에 대해 논의할 때도 이 스킬을 참조하여 규칙을 준수한다.
---

# adr-manage

ADR 생성 및 수정 시 `docs/adr/README.md`의 작성 규칙을 자동으로 적용한다.

## 트리거

- 새 ADR 생성 요청
- 기존 ADR 수정/업데이트
- ADR 내용에 대한 논의 중 직접 편집이 필요할 때

## Workflow

### 1. README 규칙 로드

작업 전 반드시 `docs/adr/README.md`의 **작성 규칙** 섹션을 읽는다. 이 문서가 ADR 작성의 source of truth이다.

### 2. 새 ADR 생성

1. 대상 카테고리의 기존 ADR 번호를 확인하여 다음 번호를 결정한다
2. README.md의 템플릿 구조를 따른다: Status, Context, Decision, Consequences, Related
3. 아래 검증 규칙을 적용하여 초안을 작성한다
4. `docs/adr/README.md` 카테고리별 목록에 새 항목을 추가한다
5. 필요하면 `docs/adr/README.md`의 디렉토리 구조에 새 카테고리를 추가한다

### 3. 기존 ADR 수정

1. 수정 대상 ADR을 읽는다
2. 변경 사항을 적용하면서 아래 검증 규칙을 함께 적용한다
3. 기존에 규칙을 위반하는 내용이 있으면 이번 수정 시 함께 정리한다 (점진적 정리 원칙)
4. `Updated:` 날짜를 오늘로 갱신한다
5. README.md의 해당 항목도 필요 시 갱신한다

### 4. 검증 규칙

모든 ADR 작성/수정 시 아래를 검증한다.

#### Status 값 제한

유효한 값: `Proposed`, `Accepted`, `Deprecated`, `Superseded by [ADR XXXX](link)`

`Implemented`, `Done`, `Completed` 등은 유효하지 않다. `Accepted`는 "결정 확정"을 의미하며 구현 완료 여부와 무관하다. 구현 진행 상태를 표기해야 하면 괄호로 부연한다: `Accepted (Phase 1 완료)`.

#### 구현 세부사항 배제 (리트머스 테스트)

> "이 값/세부사항이 코드에서 바뀌면, 아키텍처 결정 자체가 바뀌는가?"
> **NO** -> ADR에 넣지 않는다. **YES** -> ADR에 유지한다.

금지 항목:
- 구현 파일 경로 (e.g., `packages/agent/src/...`)
- 코드 스니펫 (Python 클래스, TS 인터페이스, 함수 시그니처 등)
- DynamoDB 필드별 스키마 테이블
- 구현 상수/튜닝값 (e.g., `MAX_DEPTH = 5`)
- 마이그레이션/운영 명령어
- 전체 API JSON 요청/응답 예시

유지 항목:
- 문제 배경과 동기 (WHY)
- 결정 요약과 대안 비교
- 엔티티 관계 (개념 수준)
- 행동 규칙과 상태 전이
- 시스템 간 연동 방식
- Mermaid 다이어그램
- Consequences (긍정/부정/리스크)

#### 다이어그램 내 코드 참조

Mermaid 다이어그램 안에서도 함수명 대신 동작을 서술한다.
- Bad: `agent.generate_hypotheses(scoping_result)`
- Good: `스코핑 결과 기반 가설 생성`

#### API 섹션

API 엔드포인트 목록(Method, Path, 설명)은 아키텍처 결정의 일부이므로 유지한다.

#### 한국어 작성

ADR 본문은 한국어로 작성한다. 기술 용어, 코드 식별자, 영문 고유명사는 원어 그대로 쓴다.

### 5. 최종 확인

작성/수정 완료 후:
1. Status 값이 유효한지 확인
2. 코드 스니펫이나 파일 경로가 남아 있지 않은지 확인
3. README.md 인덱스가 갱신되었는지 확인
4. Related 섹션의 링크가 유효한지 확인
