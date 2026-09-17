# Contributing Guide

이 문서는 RCA Agent(AWS 기반 자동 RCA 분석 에이전트) 프로젝트에 기여할 때 따라야 하는 규칙을 정의합니다.

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

| Scope         | 대상                                                      |
| ------------- | --------------------------------------------------------- |
| `agent`       | RCA 에이전트 코어 (`packages/agent/`)                     |
| `headless-codex` | Codex on Bedrock Runtime headless 에이전트 (`packages/headless-codex/`) |
| `infra`       | AWS CDK 인프라 (`packages/infra/`)                        |
| `sensor`      | 헬스케어 센서 앱 (`packages/healthcare-sensor-app/`)      |
| `dashboard`   | RCA 대시보드 (`packages/dashboard/`)                      |
| `workspace`   | 워크스페이스 루트 설정 (`nx.json`, `package.json` 등)     |
| `deps`        | 의존성 관리 (`package.json`, `pnpm-lock.yaml`)            |
| `docs`        | 문서 (`docs/`, PRD, ADR)                                  |

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
- **그래프**: Vue Flow + dagre
- **컴포넌트**: Vue 3 Composition API (`<script setup lang="ts">`)
- **네이밍**: 컴포넌트 PascalCase, 변수/함수 camelCase

### Python (Agent, Healthcare Sensor App)

- **Python 버전**: 3.13+
- **패키지 관리**: uv (`pyproject.toml`)
- **린터/포매터**: ruff (`line-length=120`, `target-version="py313"`)
- **타입 힌트**: Pydantic 모델 + `from __future__ import annotations`
- **테스트**: pytest (`uv run pytest tests/`)
- **환경 설정**: python-dotenv (`env/local.env`)
- **네이밍**: 함수/변수 snake_case, 클래스 PascalCase

```bash
# 린트 검사
uv run ruff check src/

# 자동 포매팅
uv run ruff format src/

# 테스트
uv run pytest tests/ -x -q
```

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

### 자동화 훅 (Codex)

개발 지침은 루트와 패키지의 `AGENTS.md`를 사용합니다. 프로젝트 훅 설정은
`.codex/hooks.json`, 구현은 `scripts/hooks/`에 있습니다. 저장소 스킬은
`.agents/skills/`에 두며, 장애 주입 스킬은 현재 단일 컬럼 오류 데모를 따릅니다.

| 훅 | 시점 | 동작 |
| --- | --- | --- |
| `format-file.sh` | Codex `apply_patch` 완료 후 | 패치에 포함된 추가·수정·이동 대상 파일을 각 패키지의 Ruff 또는 저장소 Prettier로 포맷 |
| `verify-before-push.sh` | Codex 셸의 `git push` 실행 전 | 저장소 루트에서 `pnpm verify` 실행, 실패하면 push 차단 |

Codex CLI의 `/hooks`에서 프로젝트 훅 두 개의 내용을 검토하고 신뢰해야 실행됩니다.
새 설정을 저장한 것만으로 신뢰가 부여되지는 않습니다. 훅이 로드되지 않는 세션이나
일반 터미널에서는 `pnpm verify`를 직접 실행하세요. 셸·스크립트로 수정한 파일은
패치 후 포맷 훅의 대상이 아니므로 해당 포매터를 직접 실행해야 합니다.
훅은 보조 검사이며 CI의 동일한 검증을 대체하지 않습니다.

포맷 규칙은 각 패키지의 `pyproject.toml`, `.prettierrc`와 `.prettierignore`가
소유합니다. 훅은 작업 디렉터리가 하위 패키지여도 저장소 루트를 찾아 실행합니다.
사내 git-defender가 사용하는 `core.hooksPath`와 전역 Codex 설정은 변경하지 않습니다.
모델 평가가 pending이면 검증 성공으로 취급하지 않습니다.

Codex 훅의 설정·신뢰·입출력 규격은 [공식 문서](https://learn.chatgpt.com/docs/hooks)를
따릅니다. 과거 테스트 보고서의 Claude Code 기록은 당시 실행 이력으로 보존합니다.

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
