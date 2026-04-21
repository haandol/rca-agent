# Contributing Guide

이 문서는 ui-workshop 프로젝트에 기여할 때 따라야 하는 규칙을 정의합니다.

## 목차

- [커밋 메시지 규칙](#커밋-메시지-규칙)
- [브랜치 전략](#브랜치-전략)
- [코드 스타일](#코드-스타일)
- [테스트](#테스트)
- [Pull Request](#pull-request)

---

## 커밋 메시지 규칙

[Conventional Commits v1.0.0](https://www.conventionalcommits.org/en/v1.0.0/) 스펙을 따릅니다.

### 형식

```
<type>(<scope>): <subject>

[body]

[footer(s)]
```

### Type (필수)

| Type       | 용도                                      | SemVer 영향 |
| ---------- | ----------------------------------------- | ----------- |
| `feat`     | 새로운 기능 추가                          | MINOR       |
| `fix`      | 버그 수정                                 | PATCH       |
| `refactor` | 기능 변경 없는 코드 리팩토링              | -           |
| `docs`     | 문서 변경                                 | -           |
| `test`     | 테스트 추가/수정                          | -           |
| `chore`    | 빌드 설정, 의존성 업데이트 등 유지보수    | -           |
| `style`    | 코드 포매팅, 세미콜론 등 (로직 변경 없음) | -           |
| `perf`     | 성능 개선                                 | -           |
| `ci`       | CI/CD 설정 변경                           | -           |
| `build`    | 빌드 시스템, 외부 의존성 변경             | -           |

### Scope (선택)

변경 대상 모듈을 괄호 안에 명시합니다. 이 프로젝트의 주요 scope:

| Scope             | 대상                                                  |
| ----------------- | ----------------------------------------------------- |
| `web`             | 웹 프론트엔드 (`packages/web/`)                       |
| `prd-writer`      | PRD 문서 작성 MCP 서버 (`packages/prd-writer/`)       |
| `asset-generator` | 에셋 생성 MCP 서버 (`packages/asset-generator/`)      |
| `workshop`        | 워크숍 자료 및 문서 (`packages/workshop/`)            |
| `workspace`       | 워크스페이스 루트 설정 (`nx.json`, `package.json` 등) |
| `deps`            | 의존성 관리 (`package.json`, `pnpm-lock.yaml`)        |
| `scripts`         | 스크립트 (`scripts/`)                                 |

### Subject (필수)

- 영문 소문자로 시작
- 명령형(imperative mood) 사용: "add", "fix", "change" (O) / "added", "fixes", "changed" (X)
- 마침표 생략
- 50자 이내 권장 (72자 이내 필수)

### Body (선택)

- subject에서 설명이 부족할 때 **왜(why)** 변경했는지 작성
- 빈 줄로 subject와 구분
- 한 줄 72자 이내로 줄바꿈

### Footer (선택)

- `BREAKING CHANGE: <설명>` — 하위 호환성 깨지는 변경 (SemVer MAJOR)
- `Refs: #<이슈번호>` — 관련 이슈 참조
- `Co-Authored-By: Name <email>` — 공동 작성자

### Breaking Change 표기

타입 뒤에 `!`를 붙이거나 footer에 `BREAKING CHANGE:`를 사용합니다:

```
feat(my-lib)!: remove deprecated API

BREAKING CHANGE: legacyApi()가 제거되었습니다.
newApi()로 마이그레이션하세요.
```

### 좋은 예시

```
feat(my-app): add user authentication flow

OAuth 2.0 기반 인증 플로우를 구현합니다.
로그인, 로그아웃, 토큰 갱신을 지원합니다.

Refs: #12
```

```
fix(my-lib): correct date formatting for locale ko-KR
```

```
refactor(my-app): extract form validation into shared utility
```

```
chore(deps): bump nx to 22.6.0
```

```
docs: add contributing guide
```

### 나쁜 예시

```
# type 없음
Update button styles

# 과거형 사용
feat: Added support for dark mode

# 너무 모호함
update components
fix stuff

# 여러 변경을 한 커밋에 섞음
feat(my-app): add auth flow, fix header layout, update deps
```

### 원자적 커밋 (Atomic Commits)

하나의 커밋에는 하나의 논리적 변경만 포함합니다:

- 기능 추가와 버그 수정을 같은 커밋에 넣지 않습니다
- 리팩토링과 기능 변경을 같은 커밋에 넣지 않습니다
- 변경이 크면 여러 커밋으로 나눕니다

---

## 브랜치 전략

### 브랜치 명명 규칙

```
<type>/<short-description>
```

| 접두사      | 용도             | 예시                        |
| ----------- | ---------------- | --------------------------- |
| `feat/`     | 새 기능 개발     | `feat/user-auth`            |
| `fix/`      | 버그 수정        | `fix/login-redirect`        |
| `refactor/` | 리팩토링         | `refactor/shared-utils`     |
| `docs/`     | 문서 작업        | `docs/contributing-guide`   |
| `chore/`    | 유지보수         | `chore/update-dependencies` |
| `test/`     | 테스트 추가/수정 | `test/auth-coverage`        |

### 워크플로우

1. `main`에서 새 브랜치 생성
2. 작업 후 커밋 (위 커밋 규칙 준수)
3. Pull Request 생성
4. 리뷰 후 `main`에 머지

```bash
git checkout main
git pull origin main
git checkout -b feat/my-feature
# ... 작업 ...
git add <files>
git commit -m "feat(scope): add my feature"
git push -u origin feat/my-feature
```

---

## 코드 스타일

### TypeScript (Web)

- **프레임워크**: Nuxt 4 | **스타일링**: TailwindCSS 4 + DaisyUI 5
- **상태 관리**: Pinia
- **컴포넌트**: Vue 3 Composition API (`<script setup lang="ts">`)
- **네이밍**: 컴포넌트 PascalCase, 변수/함수 camelCase

### TypeScript (MCP 서버)

- **TypeScript 버전**: ~5.9 (`tsconfig.base.json` 참조)
- **Strict 모드**: 활성화 (`"strict": true`)
- **타입 힌트**: 모든 함수에 반환 타입 명시
- **모듈 시스템**: NodeNext (`"module": "nodenext"`)

### 포매팅

Prettier를 사용합니다:

- 싱글 쿼트 사용 (`"singleQuote": true`)

```bash
# 포매팅 검사
pnpm prettier --check .

# 자동 수정
pnpm prettier --write .
```

### 프로젝트 구조

Nx 모노레포의 `packages/*` 구조를 따릅니다:

```
packages/
├── my-app/          # 애플리케이션
│   ├── src/
│   ├── package.json
│   ├── project.json
│   └── tsconfig.json
└── my-lib/          # 라이브러리
    ├── src/
    ├── package.json
    ├── project.json
    └── tsconfig.json
```

새 패키지 추가 시 Nx 제너레이터를 사용합니다:

```bash
# 예: React 앱 추가
pnpm nx g @nx/react:app packages/my-app

# 예: 라이브러리 추가
pnpm nx g @nx/js:lib packages/my-lib
```

---

## 테스트

```bash
# 전체 테스트
pnpm nx run-many -t test

# 특정 프로젝트 테스트
pnpm nx test <project-name>

# 영향받은 프로젝트만 테스트
pnpm nx affected -t test
```

### 테스트 규칙

- 새 기능 추가 시 관련 테스트 파일 작성
- 외부 API 호출은 반드시 mock 처리
- `pnpm nx affected -t test`로 변경 영향 범위 테스트

---

## Pull Request

### PR 제목

커밋 메시지와 동일한 Conventional Commits 형식을 사용합니다:

```
feat(my-app): add user authentication flow
```

### PR 본문 템플릿

```markdown
## Summary

변경 사항을 1~3개 bullet point로 요약합니다.

## Motivation

왜 이 변경이 필요한지 설명합니다.

## Changes

- 주요 변경 사항 상세 목록

## Test Plan

- [ ] 기존 테스트 통과 확인 (`pnpm nx affected -t test`)
- [ ] 새 테스트 추가 (해당 시)
- [ ] 수동 테스트 시나리오 설명
```

### 머지 규칙

- Squash merge를 기본으로 사용합니다
- 머지 커밋 메시지는 Conventional Commits 형식을 따릅니다
- `main` 브랜치에 직접 push하지 않습니다
