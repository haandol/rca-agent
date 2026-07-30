#!/usr/bin/env bash
# ADR 의존성 단방향 검사 — pre-commit / CI 하드 게이트용.
#
# ADR 의존성은 단방향이고 참조는 어느 방향으로도 직접 적지 않는다:
#   (a) 코드·비-ADR 문서 → ADR 역참조 금지. ADR 번호는 split/rollup/supersede로
#       이동하므로, 코드가 번호를 들고 있으면 결정이 바뀌지 않았는데도 구조 변경이
#       코드 수정을 줄줄이 강제한다.
#   (b) ADR 본문 → PRD 역참조 금지. ADR은 import 시점에 PRD의 동기를 한 번 흡수한
#       뒤로는 PRD를 다시 가리키지 않는다.
#
# 플러그인이 제공하는 동일 검사와 정규식은 같지만, 이 스크립트는 스캔 범위를 이
# 저장소에 맞춰 좁힌다 — 빌드 캐시와 시점 스냅샷 리포트는 결정 문서가 아니므로
# 게이트 판정에서 제외한다.
set -uo pipefail

fail=0

# 결정 문서가 아니어서 역참조 판정 대상이 아닌 경로.
#   .nx/           — 빌드 캐시. 파일 목록을 그대로 담아 모든 ADR 경로가 등장한다.
#   docs/test-reports/ — 특정 시점의 리뷰·E2E 기록. 그 시점의 ADR 번호를 인용하는
#                        것이 기록의 목적이므로 고쳐선 안 된다.
#   이 스크립트 자체 — 검사 정규식이 곧 위반 패턴이다.
EXCLUDED_PATHS='^\./(\.nx|docs/test-reports|node_modules|\.git|dist|cdk\.out)/|^\./scripts/adr-invariants\.sh:'

echo "== (a) 코드 → ADR 역참조 =="
hits_a=$(
  grep -rInE 'ADR [a-z][a-z-]*/[0-9]{4}|docs/adr/[a-z][a-z-]*|ADR_REF' . \
    --exclude-dir=node_modules --exclude-dir=.git --exclude-dir=.venv \
    --exclude-dir=__pycache__ --exclude-dir=dist --exclude-dir=cdk.out \
    --exclude-dir=.nx --exclude-dir=.pytest_cache --exclude-dir=.ruff_cache \
    2>/dev/null \
  | grep -vE '^\./docs/adr/' \
  | grep -vE "$EXCLUDED_PATHS" \
  || true
)
if [ -n "$hits_a" ]; then
  echo "$hits_a"
  echo "→ 코드·문서에서 ADR 번호를 제거하라. ADR 인덱스는 docs/adr/.mapping.json 이다."
  fail=1
else
  echo "OK (0건)"
fi

echo
echo "== (b) ADR → PRD 역참조 =="
hits_b=$(
  grep -rInE '\.alps\.xml|ALPS Section|Section [0-9]+\.[0-9]+|#F-[A-Z]' docs/adr \
    --include='[0-9][0-9][0-9][0-9]-*.md' 2>/dev/null || true
)
if [ -n "$hits_b" ]; then
  echo "$hits_b"
  echo "→ ADR 본문에서 PRD 인용을 제거하라. 옮길 곳은 없다 — ADR이 결정의 권위다."
  fail=1
else
  echo "OK (0건)"
fi

exit "$fail"
