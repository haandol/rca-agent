#!/usr/bin/env bash
# Codex apply_patch 결과의 파일을 저장소 포매터로 정리한다.
set -uo pipefail
exec python3 "$(dirname -- "${BASH_SOURCE[0]}")/format_patch.py"
