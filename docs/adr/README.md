# Architecture Decision Records (ADR)

이 디렉토리는 UI PoC Workshop 프로젝트의 주요 아키텍처 결정을 문서화합니다.

## ADR이란?

Architecture Decision Record (ADR)는 소프트웨어 개발 과정에서 내린 중요한 아키텍처 결정을 기록하는 문서입니다. 각 ADR은 다음을 포함합니다:

- **Context**: 결정이 필요했던 배경과 문제
- **Decision**: 내린 결정과 그 이유
- **Consequences**: 결정의 긍정적/부정적 영향

## 디렉토리 구조

```
adr/
├── web/              # 웹 프론트엔드 관련 결정
├── prd-writer/       # PRD Writer MCP 서버 관련 결정
├── asset-generator/  # Asset Generator MCP 서버 관련 결정
├── workshop/         # 워크숍 운영/콘텐츠 관련 결정
└── infra/            # 인프라/빌드/배포 관련 결정
```

## 카테고리별 ADR 목록

### Web

(아직 등록된 ADR 없음)

### PRD Writer

(아직 등록된 ADR 없음)

### Asset Generator

(아직 등록된 ADR 없음)

### Workshop

(아직 등록된 ADR 없음)

### Infra

(아직 등록된 ADR 없음)

## ADR 작성 가이드

새로운 ADR을 작성할 때는 `TEMPLATE.md` 템플릿을 사용하세요.

## 작성 규칙

- ADR에는 **구현 파일 경로를 포함하지 않는다.** ADR은 아키텍처 결정(Context, Decision, Consequences)을 기록하는 문서이며, 실제 수정할 파일 목록은 구현 시점에 코드베이스를 직접 확인하여 결정한다. 파일 경로는 리팩토링/이동에 의해 쉽게 무효화되므로 ADR의 유지보수 부담을 줄이기 위해 제외한다.
- 기존 ADR 중 구현 파일 경로가 포함된 것은 해당 ADR이 업데이트될 때 점진적으로 제거한다.

## 명명 규칙

- 파일명: `XXXX-kebab-case-title.md`
- 번호는 카테고리 내에서 순차적으로 증가
- 제목은 명확하고 간결하게

## 참고

- [ADR GitHub](https://adr.github.io/)
