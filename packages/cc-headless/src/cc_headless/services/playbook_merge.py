"""회고 갱신의 병합 규칙.

"추가·교정만 하고 삭제하지 않는다"는 규칙을 프롬프트 지시가 아니라 코드에 둔다.
프롬프트 지시만으로는 모델이 필드를 누락할 때 축적이 조용히 사라진다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_EXECUTION_STEP_FIELDS = ("step_id", "intent", "action", "success_criteria")

VERIFICATION_STATUS_FIELD = "verification_status"
PLAYBOOK_DRAFT = "DRAFT"
PLAYBOOK_VERIFIED = "VERIFIED"
_KNOWN_STATUSES = frozenset({PLAYBOOK_DRAFT, PLAYBOOK_VERIFIED})


def normalize_verification_status(raw: object) -> str:
    """기록된 검증 상태를 복원한다. 해석할 수 없는 값은 초안이다.

    값이 없거나 알 수 없다는 것은 승격 이전 레코드이거나 신뢰할 수 없는 출처가 쓴 것이며,
    실행으로 입증되지 않은 절차가 검증됨으로 읽히면 사람이 승인 판단을 잘못한다.
    """
    if isinstance(raw, str) and raw in _KNOWN_STATUSES:
        return raw
    return PLAYBOOK_DRAFT


# 모델이 갱신안에 담아도 무시하는 필드. 검증 상태는 서버가 소유하므로 LLM 출력이
# 검증 여부의 권위가 되면 실행되지 않은 절차가 검증됨으로 표기된다. 식별자와 stage는
# 병합이 유지해야 하는 값이라 갱신안이 바꿀 수 없다.
_SERVER_OWNED_FIELDS = frozenset({"execution_steps", "playbook_id", "stage", "verification_status"})


@dataclass
class PlaybookDiff:
    """무엇이 어떻게 바뀌었는지. 회고 결과의 타당성을 대조하는 근거다."""

    changed_fields: list[str] = field(default_factory=list)
    corrected_steps: list[dict] = field(default_factory=list)
    added_steps: list[str] = field(default_factory=list)
    preserved_steps: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.changed_fields or self.corrected_steps or self.added_steps)

    def to_dict(self) -> dict:
        return {
            "changed_fields": self.changed_fields,
            "corrected_steps": self.corrected_steps,
            "added_steps": self.added_steps,
            "preserved_steps": self.preserved_steps,
        }


def _meaningful(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not None


def _merge_step(existing: dict, update: dict) -> tuple[dict, dict | None]:
    """한 절차를 교정한다. 갱신이 비운 필드는 기존 값을 유지한다."""
    merged = dict(existing)
    changes: dict[str, dict[str, str]] = {}
    for name in _EXECUTION_STEP_FIELDS:
        if name == "step_id":
            continue
        new_value = update.get(name)
        if not _meaningful(new_value):
            continue
        old_value = existing.get(name, "")
        if str(new_value) == str(old_value):
            continue
        merged[name] = new_value
        changes[name] = {"before": str(old_value), "after": str(new_value)}
    if not changes:
        return merged, None
    return merged, {"step_id": existing.get("step_id", ""), "changes": changes}


def merge_playbook_update(existing: dict, update: object) -> tuple[dict, PlaybookDiff]:
    """기존 플레이북에 회고의 갱신안을 병합한다.

    삭제는 일어나지 않는다. 갱신안에 없는 필드와 절차는 기존 값이 그대로 남고,
    절차 순서는 기존 순서를 유지한 뒤 새 절차만 뒤에 붙는다 — 순서를 재배치하면 과거
    실행 증거가 가리키는 절차를 찾을 수 없다.
    """
    merged = dict(existing)
    diff = PlaybookDiff()

    if not isinstance(update, dict):
        # 갱신안을 해석할 수 없으면 아무것도 바꾸지 않는다. 잘못된 갱신보다 미갱신이
        # 안전하다.
        return merged, diff

    existing_steps = existing.get("execution_steps")
    existing_steps = existing_steps if isinstance(existing_steps, list) else []
    existing_by_id = {
        str(step.get("step_id")): step for step in existing_steps if isinstance(step, dict) and step.get("step_id")
    }

    for name, new_value in update.items():
        if name in _SERVER_OWNED_FIELDS:
            continue
        if not _meaningful(new_value):
            continue
        if str(existing.get(name, "")) == str(new_value):
            continue
        merged[name] = new_value
        diff.changed_fields.append(name)

    update_steps = update.get("execution_steps")
    update_steps = update_steps if isinstance(update_steps, list) else []
    update_by_id: dict[str, dict] = {}
    for step in update_steps:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("step_id") or "").strip()
        if step_id:
            update_by_id[step_id] = step

    merged_steps: list[dict] = []
    for step_id, existing_step in existing_by_id.items():
        update_step = update_by_id.get(step_id)
        if update_step is None:
            merged_steps.append(existing_step)
            diff.preserved_steps.append(step_id)
            continue
        merged_step, change = _merge_step(existing_step, update_step)
        merged_steps.append(merged_step)
        if change is None:
            diff.preserved_steps.append(step_id)
        else:
            diff.corrected_steps.append(change)

    for step_id, update_step in update_by_id.items():
        if step_id in existing_by_id:
            continue
        added = {name: update_step.get(name, "") for name in _EXECUTION_STEP_FIELDS}
        added["step_id"] = step_id
        # 새 절차도 실행 근거가 되므로 관측 기준 없이는 받지 않는다.
        if not _meaningful(added.get("action")) or not _meaningful(added.get("success_criteria")):
            continue
        merged_steps.append(added)
        diff.added_steps.append(step_id)

    merged["execution_steps"] = merged_steps
    return merged, diff


def promote_to_verified(playbook: dict) -> dict:
    """실행으로 입증된 절차를 검증됨으로 승격한다.

    해결된 실행의 회고가 갱신을 반영하는 지점에서만 호출한다 — 해결 판정과 절차 교정을
    모두 통과한 곳이 여기뿐이다. 이미 검증됨이면 그대로 둔다. 되돌리는 전이는 없으므로
    이 함수에 반대 방향은 없다.
    """
    promoted = dict(playbook)
    promoted[VERIFICATION_STATUS_FIELD] = PLAYBOOK_VERIFIED
    return promoted
