#!/usr/bin/env bash
# 편집된 파일 하나를 그 파일의 소유 설정대로 포맷한다 — Claude Code PostToolUse 훅.
#
# 이 저장소는 `format:check` 를 CI 하드 게이트로 두는데, 포맷은 사람이 기억해서
# 맞추는 종류의 규칙이 아니다. 실제로 5개 파일이 88자 기준으로 줄바꿈된 채 남아
# 게이트를 오래 깨뜨렸고, 그 사실은 커밋 시점이 아니라 CI 에서야 드러났다.
# 편집 직후에 고치면 포맷은 커밋 이력에서 사라지고 리뷰는 내용만 본다.
#
# 포매터를 파일별로 한 번씩만 부르는 이유는 도구가 각 파일 위치의 설정을 스스로
# 찾기 때문이다 — 두 Python 패키지는 line-length 120 을 pyproject.toml 에 두고,
# prettier 는 .prettierrc 와 .prettierignore 를 본다. 그래서 이 스크립트는 규칙을
# 하나도 갖고 있지 않다. 규칙을 여기에 적으면 설정과 갈라진다.
#
# 실패해도 편집을 되돌리지 않는다. 포맷은 편집의 성공 조건이 아니고, 포매터가 없는
# 환경에서 편집이 막히면 훅이 도구가 아니라 장애물이 된다.
set -uo pipefail

file=$(jq -r '.tool_response.filePath // .tool_input.file_path // empty' 2>/dev/null)
[ -n "$file" ] && [ -f "$file" ] || exit 0

root="${CLAUDE_PROJECT_DIR:-.}"

case "$file" in
  *.py)
    command -v ruff >/dev/null 2>&1 || exit 0
    ruff format -q -- "$file" || true
    # 임포트 순서는 포맷이 아니라 린트 규칙(I)이지만 기계적으로 고칠 수 있고,
    # 고치지 않으면 lint 게이트가 대신 실패한다.
    ruff check -q --fix-only --select I -- "$file" || true
    ;;
  *.ts | *.tsx | *.vue | *.mjs | *.cjs | *.js | *.json | *.md | *.yml | *.yaml)
    prettier="$root/node_modules/.bin/prettier"
    [ -x "$prettier" ] || exit 0
    # --ignore-unknown 은 확장자 밖의 입력을, .prettierignore 는 생성물을 걸러낸다.
    "$prettier" --write --ignore-unknown --log-level warn -- "$file" || true
    ;;
esac

exit 0
