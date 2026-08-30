"""실행 명령의 파괴성 판정.

판정은 서버가 수행한다. 프롬프트 지시로 세운 경계는 우회될 수 있으므로, 실행 도구가
명령을 파싱해 작업 이름을 추출하고 거부 어휘와 대조한 뒤 실행 여부를 결정한다.
거부 어휘 자체는 `destructive_actions` 가 보유하며 이 모듈은 그 어휘를 명령에
적용한다.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from codex_headless.services.destructive_actions import (
    OUT_OF_SCOPE_SERVICE_PREFIXES,
    UndecidableCommandError,
    classify_command,
    is_destructive_operation,
    is_self_control_operation,
)

# 실행 도구는 AWS 제어 평면 호출만 수행한다. 다른 실행 파일은 작업 이름을 추출할 수
# 없으므로 판정 불가이며, 판정 불가를 허용으로 해석하면 거부 목록이 무력화된다.
_ALLOWED_EXECUTABLE = "aws"


@dataclass(frozen=True)
class GateVerdict:
    allowed: bool
    reason: str = ""
    service: str = ""
    operation: str = ""
    argv: tuple[str, ...] = ()
    # 판정 자체가 불가능했는지. 파괴적이라고 확인된 거부와 구별해 증거에 남긴다.
    undecidable: bool = False


def _refused(reason: str, *, undecidable: bool = False) -> GateVerdict:
    return GateVerdict(allowed=False, reason=reason, undecidable=undecidable)


def evaluate_command(command: str) -> GateVerdict:
    """명령을 실행해도 되는지 판정한다.

    두 단계로 본다. 먼저 명령을 argv 로 분해해 실행 파일과 작업 이름의 위치를
    확정하고, 그다음 그 작업 이름을 거부 어휘와 대조한다. 어휘 대조만으로는 문자열
    어딘가에 나타난 무해한 작업 이름을 근거로 파괴적 호출이 통과할 수 있다.
    """
    if not isinstance(command, str) or not command.strip():
        return _refused("command is empty", undecidable=True)

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return _refused(f"command could not be parsed: {exc}", undecidable=True)
    if len(argv) < 3:
        return _refused("command does not name an AWS service and operation", undecidable=True)
    if argv[0] != _ALLOWED_EXECUTABLE:
        return _refused(
            f"only {_ALLOWED_EXECUTABLE} control-plane calls can be classified, got {argv[0]}",
            undecidable=True,
        )
    # 서비스·작업 앞에 오는 전역 옵션은 값을 갖는 것과 갖지 않는 것을 구별해야 위치를
    # 알 수 있고, 그 판단은 CLI 의 옵션 표를 알아야 가능하다. 위치를 확정할 수 없으면
    # 뒤에 오는 작업 이름이 실제로 실행되는 작업인지 알 수 없으므로 판정 불가다.
    if argv[1].startswith("-") or argv[2].startswith("-"):
        return _refused(
            "global options before the service and operation make the operation position undecidable",
            undecidable=True,
        )

    try:
        service, operation = classify_command(command)
    except UndecidableCommandError as exc:
        return _refused(f"command could not be classified: {exc}", undecidable=True)

    # argv 가 가리키는 작업과 어휘 대조가 본 작업이 다르면 어느 쪽이 실제로 실행될지
    # 알 수 없다. 판정 불가로 처리한다.
    if (argv[1], argv[2]) != (service, operation):
        return _refused(
            "command's leading service and operation disagree with the classified pair",
            undecidable=True,
        )

    if service.lower() in OUT_OF_SCOPE_SERVICE_PREFIXES:
        return GateVerdict(
            allowed=False,
            reason=f"{service} is outside the execution scope",
            service=service,
            operation=operation,
            argv=tuple(argv),
        )
    if is_self_control_operation(service, operation):
        return GateVerdict(
            allowed=False,
            reason=f"{service} {operation} is a self-control or privilege-escalation operation",
            service=service,
            operation=operation,
            argv=tuple(argv),
        )
    if is_destructive_operation(service, operation):
        return GateVerdict(
            allowed=False,
            reason=f"{service} {operation} is an irreversible operation",
            service=service,
            operation=operation,
            argv=tuple(argv),
        )

    return GateVerdict(
        allowed=True,
        service=service,
        operation=operation,
        argv=tuple(argv),
    )
