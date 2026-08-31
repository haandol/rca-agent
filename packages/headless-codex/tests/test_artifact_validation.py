import json

import pytest

from headless_codex.services.artifact_validation import (
    _PLAYBOOK_LIST_FIELDS,
    _PLAYBOOK_STRING_FIELDS,
    _REPORT_SECTIONS,
    ArtifactValidationError,
    validate_completion_artifacts,
    validate_validation_artifacts,
)


def _hypothesis(
    hypothesis_id: str,
    *,
    parent_id: str | None = None,
    depth: int = 0,
    tree_id: str = "tree-1",
    fault_type: str = "db-leak",
) -> dict:
    return {
        "hypothesis_id": hypothesis_id,
        "tree_id": tree_id,
        "title": f"hypothesis {hypothesis_id}",
        "description": f"description for {hypothesis_id}",
        "fault_type": fault_type,
        "category": "INFRASTRUCTURE",
        "confidence_score": 0.7,
        "required_evidence": ["metric"],
        "status": "PENDING",
        "parent_id": parent_id,
        "depth": depth,
    }


def _validation(
    loop_index: int,
    *,
    confirmed: list[dict] | None = None,
    rejected: list[dict] | None = None,
    needs_investigation: list[dict] | None = None,
    closed: list[dict] | None = None,
    new_hypotheses: list[dict] | None = None,
) -> dict:
    return {
        "stage": "VALIDATION",
        "loop_index": loop_index,
        "confirmed": confirmed or [],
        "rejected": rejected or [],
        "needs_investigation": needs_investigation or [],
        "closed": closed or [],
        "new_hypotheses": new_hypotheses or [],
        "summary": f"validation loop {loop_index}",
        "output_summary": f"validation loop {loop_index} complete",
    }


def _write_json(path, value: dict) -> None:
    path.write_text(json.dumps(value))


def _result(hypothesis_id: str, *, fault_type: str | None = None, confidence: float = 0.95) -> dict:
    result = {
        "hypothesis_id": hypothesis_id,
        "confidence": confidence,
        "reasoning": f"{hypothesis_id} classified",
    }
    if fault_type is not None:
        result["fault_type"] = fault_type
    return result


@pytest.fixture
def artifact_dir(tmp_path):
    _write_json(
        tmp_path / "hypotheses.json",
        {
            "stage": "HYPOTHESIS_GENERATION",
            "tree_id": "tree-1",
            "hypotheses": [_hypothesis("root")],
            "summary": "root hypothesis",
            "output_summary": "one root hypothesis",
        },
    )
    return tmp_path


def test_later_validation_can_confirm_a_child_branched_in_an_earlier_loop(artifact_dir):
    # The known-hypothesis set has to accumulate across loops: a child created in
    # loop 1 is a legitimate target for loop 2's verdict.
    child = _hypothesis("child", parent_id="root", depth=1)
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(1, new_hypotheses=[child]),
    )
    _write_json(
        artifact_dir / "validation-2.json",
        _validation(2, confirmed=[_result("child", fault_type="db-leak")]),
    )

    path, artifact = validate_validation_artifacts(artifact_dir)

    assert path.name == "validation-2.json"
    assert [entry["hypothesis_id"] for entry in artifact["confirmed"]] == ["child"]


@pytest.mark.parametrize(
    "missing_field",
    [
        "hypothesis_id",
        "tree_id",
        "title",
        "description",
        "fault_type",
        "category",
        "confidence_score",
        "required_evidence",
        "status",
        "parent_id",
        "depth",
    ],
)
def test_new_hypothesis_requires_all_schema_fields(artifact_dir, missing_field):
    child = _hypothesis("child", parent_id="root", depth=1)
    child.pop(missing_field)
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(1, new_hypotheses=[child]),
    )

    with pytest.raises(ArtifactValidationError):
        validate_validation_artifacts(artifact_dir)


def test_new_hypothesis_rejects_parent_from_future_loop(artifact_dir):
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(
            1,
            new_hypotheses=[
                _hypothesis("child", parent_id="future-parent", depth=2),
            ],
        ),
    )
    _write_json(
        artifact_dir / "validation-2.json",
        _validation(
            2,
            new_hypotheses=[
                _hypothesis("future-parent", parent_id="root", depth=1),
            ],
        ),
    )

    with pytest.raises(ArtifactValidationError, match="validation-1.json new hypothesis parent_id is unknown"):
        validate_validation_artifacts(artifact_dir)


def test_new_hypothesis_rejects_unknown_parent(artifact_dir):
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(
            1,
            new_hypotheses=[
                _hypothesis("child", parent_id="missing", depth=1),
            ],
        ),
    )

    with pytest.raises(ArtifactValidationError, match="new hypothesis parent_id is unknown"):
        validate_validation_artifacts(artifact_dir)


def test_new_hypothesis_rejects_duplicate_id_from_previous_loop(artifact_dir):
    child = _hypothesis("child", parent_id="root", depth=1)
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(1, new_hypotheses=[child]),
    )
    _write_json(
        artifact_dir / "validation-2.json",
        _validation(2, new_hypotheses=[child]),
    )

    with pytest.raises(ArtifactValidationError, match="new hypothesis IDs must be unique"):
        validate_validation_artifacts(artifact_dir)


@pytest.mark.parametrize(
    ("tree_id", "depth", "error"),
    [
        ("wrong-tree", 1, "new hypothesis tree_id is invalid"),
        ("tree-1", 2, "new hypothesis depth must equal parent depth \\+ 1"),
    ],
)
def test_new_hypothesis_rejects_invalid_tree_or_depth(artifact_dir, tree_id, depth, error):
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(
            1,
            new_hypotheses=[
                _hypothesis("child", parent_id="root", depth=depth, tree_id=tree_id),
            ],
        ),
    )

    with pytest.raises(ArtifactValidationError, match=error):
        validate_validation_artifacts(artifact_dir)


# --- completion gate: one report that contains its playbook -------------------

_EVIDENCE_WINDOWS = (
    "- Current alarm window: 2026-07-29T13:00:00Z ~ 2026-07-29T14:00:00Z\n"
    "- Historical comparison window: 2026-07-29T12:00:00Z ~ 2026-07-29T13:00:00Z\n"
)


def _execution_step(step_id: str) -> dict:
    return {
        "step_id": step_id,
        "intent": f"{step_id} intent",
        "action": f"restart the healthcare service for {step_id}",
        "success_criteria": "DatabaseConnections returns below 30",
    }


def _playbook(*, steps: list[dict] | None = None, **overrides) -> dict:
    artifact: dict = {field: "value" for field in _PLAYBOOK_STRING_FIELDS}
    artifact["stage"] = "PLAYBOOK"
    artifact["verification_status"] = "DRAFT"
    artifact.update({field: [] for field in _PLAYBOOK_LIST_FIELDS})
    artifact["execution_steps"] = steps if steps is not None else [_execution_step("step-1")]
    artifact.update(overrides)
    return artifact


def _report(step_ids: list[str]) -> str:
    body = {"증거 시간 범위": _EVIDENCE_WINDOWS, "대응 플레이북": "\n".join(step_ids) or "없음"}
    return "\n".join(f"## {title}\n{body.get(title, 'placeholder')}\n" for title in _REPORT_SECTIONS)


@pytest.fixture
def confirmed_run(artifact_dir):
    """A confirmed RCA with the analysis artifacts already in place."""
    _write_json(
        artifact_dir / "scoping.json",
        {
            "stage": "SCOPING",
            "alarm_name": "alarm",
            "impact_scope": "service",
            "severity": "high",
            "summary": "s",
            "output_summary": "o",
            "metric_observations": [
                {
                    "metric_name": "DatabaseConnections",
                    "datapoints": [2, 12, 20, 27, 30],
                    "trend": "rising",
                }
            ],
            "concurrent_alarms": [],
        },
    )
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(1, confirmed=[_result("root", fault_type="db-leak")]),
    )
    return artifact_dir


def test_completion_accepts_a_report_whose_prose_matches_the_structured_steps(confirmed_run):
    _write_json(confirmed_run / "playbook.json", _playbook())
    (confirmed_run / "report.md").write_text(_report(["step-1"]))

    artifacts = validate_completion_artifacts(confirmed_run)

    assert artifacts.confirmed is True
    assert artifacts.playbook["execution_steps"][0]["step_id"] == "step-1"


def test_completion_rejects_a_report_omitting_a_structured_step(confirmed_run):
    # The prose is what a person approves; a step only in the structure would run
    # without having been approved.
    _write_json(
        confirmed_run / "playbook.json",
        _playbook(steps=[_execution_step("step-1"), _execution_step("step-2")]),
    )
    (confirmed_run / "report.md").write_text(_report(["step-1"]))

    with pytest.raises(ArtifactValidationError, match="missing execution steps: step-2"):
        validate_completion_artifacts(confirmed_run)


def test_completion_rejects_a_report_listing_steps_out_of_order(confirmed_run):
    _write_json(
        confirmed_run / "playbook.json",
        _playbook(steps=[_execution_step("step-1"), _execution_step("step-2")]),
    )
    (confirmed_run / "report.md").write_text(_report(["step-2", "step-1"]))

    with pytest.raises(ArtifactValidationError, match="out of order"):
        validate_completion_artifacts(confirmed_run)


def test_completion_rejects_execution_steps_for_an_unconfirmed_root_cause(artifact_dir):
    # Steps proposed for an unconfirmed cause put guesswork behind the approval
    # button, so the gate refuses them rather than letting a person approve them.
    _write_json(
        artifact_dir / "scoping.json",
        {
            "stage": "SCOPING",
            "alarm_name": "alarm",
            "impact_scope": "service",
            "severity": "high",
            "summary": "s",
            "output_summary": "o",
            "metric_observations": [
                {
                    "metric_name": "DatabaseConnections",
                    "datapoints": [2, 12, 20, 27, 30],
                    "trend": "rising",
                }
            ],
            "concurrent_alarms": [],
        },
    )
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(1, needs_investigation=[_result("root")]),
    )
    _write_json(artifact_dir / "playbook.json", _playbook())
    (artifact_dir / "report.md").write_text(_report(["step-1"]))

    with pytest.raises(ArtifactValidationError, match="unconfirmed RCA must not declare"):
        validate_completion_artifacts(artifact_dir)


def test_completion_accepts_an_unconfirmed_run_without_execution_steps(artifact_dir):
    _write_json(
        artifact_dir / "scoping.json",
        {
            "stage": "SCOPING",
            "alarm_name": "alarm",
            "impact_scope": "service",
            "severity": "high",
            "summary": "s",
            "output_summary": "o",
            "metric_observations": [
                {
                    "metric_name": "DatabaseConnections",
                    "datapoints": [2, 12, 20, 27, 30],
                    "trend": "rising",
                }
            ],
            "concurrent_alarms": [],
        },
    )
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(1, needs_investigation=[_result("root")]),
    )
    _write_json(artifact_dir / "playbook.json", _playbook(steps=[]))
    (artifact_dir / "report.md").write_text(_report([]))

    artifacts = validate_completion_artifacts(artifact_dir)

    assert artifacts.confirmed is False
    assert artifacts.playbook["execution_steps"] == []


def test_completion_rejects_a_playbook_claiming_verification(confirmed_run):
    _write_json(confirmed_run / "playbook.json", _playbook(verification_status="VERIFIED"))
    (confirmed_run / "report.md").write_text(_report(["step-1"]))

    with pytest.raises(ArtifactValidationError, match="verification_status must be DRAFT"):
        validate_completion_artifacts(confirmed_run)


def test_completion_artifacts_carry_no_remediation_result(confirmed_run):
    # Execution has its own lifecycle, so a report is final at analysis time and
    # cannot be carrying an execution outcome.
    _write_json(confirmed_run / "playbook.json", _playbook())
    (confirmed_run / "report.md").write_text(_report(["step-1"]))

    artifacts = validate_completion_artifacts(confirmed_run)

    assert not hasattr(artifacts, "remediation")
    assert "remediation_result" not in artifacts.playbook
