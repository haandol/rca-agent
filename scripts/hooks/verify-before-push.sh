#!/usr/bin/env bash
# push 를 검증 뒤로 미룬다 — Claude Code PreToolUse 훅 (matcher: Bash).
#
# 이 저장소의 git hook 경로는 회사 git-defender 가 core.hooksPath 로 점유하고 있어
# pre-commit 이나 .git/hooks 를 쓸 수 없다. 그래서 push 직전 게이트를 여기에 둔다.
#
# 커밋마다가 아니라 push 에 거는 이유는, push 가 되돌리기 비싼 첫 지점이기 때문이다.
# 로컬 커밋은 고쳐 쓰면 되지만 올라간 main 은 남는다. 그리고 여기서 막히는 편이
# CI 가 몇 분 뒤에 알려주는 것보다 빠르다.
#
# 검증은 `pnpm verify` 하나로 통일한다 — CI 가 실행하는 것과 같은 명령이어야
# 로컬 통과가 CI 통과를 뜻한다. 목록을 따로 쓰면 둘이 갈라진다.
set -uo pipefail

cmd=$(jq -r '.tool_input.command // empty' 2>/dev/null)
[ -n "$cmd" ] || exit 0

# 이 명령이 git push 를 실제로 실행하는지. 세미콜론·&&·파이프로 이어붙인 절도 각각
# 본다 — `git add -A && git push` 는 push 다. `git -C dir push` 처럼 값을 갖는
# 전역 옵션도 통과시킨다. `git pushx` 같은 다른 하위명령은 걸리지 않는다.
#
# 애매하면 막는 쪽으로 기운다: 과탐의 대가는 테스트가 한 번 도는 것이고,
# 미탐의 대가는 깨진 커밋이 올라가는 것이다.
push_re='(^|[[:space:]])git([[:space:]]+(-[a-zA-Z-]+|--[a-z-]+=[^[:space:]]+)([[:space:]]+[^[:space:]-][^[:space:]]*)?)*[[:space:]]+push([[:space:]]|$)'
printf '%s' "$cmd" | awk '{ gsub(/[;&|]/, "\n"); print }' | grep -Eq "$push_re" || exit 0

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

if output=$(FORCE_COLOR= NO_COLOR=1 pnpm run verify 2>&1); then
  echo '{"systemMessage":"pnpm verify passed — push allowed"}'
  exit 0
fi

# 실패 이유를 모델에게 돌려준다. ANSI 이스케이프는 지운다 — 색상은 터미널용이고
# 여기서는 실패 원인을 덮는 잡음이다.
clean=$(printf '%s' "$output" | sed $'s/\033\\[[0-9;]*[a-zA-Z]//g')

# 어느 단계가 깨졌는지를 먼저 보여준다. nx 는 실패한 타깃을 한 곳에 모아 출력하는데,
# 그 요약은 로그 꼬리보다 위에 있어서 tail 만 남기면 잘려 나간다.
failed=$(printf '%s' "$clean" | grep -A10 'Failed tasks:' | head -12)

jq -cn \
  --arg failed "$failed" \
  --arg tail "$(printf '%s' "$clean" | tail -40)" \
  '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: (
      "`pnpm run verify` failed, so this push was blocked. Fix the failures below, then push again.\n\n"
      + (if $failed == "" then "" else $failed + "\n\n" end)
      + "--- last lines of output ---\n" + $tail
    )
  }
}'
exit 0
