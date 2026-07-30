"""Save-time shape checks must agree with the completion gate.

The save call runs while the agent can still fix its output; the completion gate
runs after the run has ended. If the two disagree about a field the artifact owns,
a run can pass every save and still fail at the end with nothing recoverable —
which is the failure this check exists to prevent.
"""

import json

import pytest

from cc_headless.services import artifact_validation
from cc_headless.services.artifact_validation import (
    ArtifactValidationError,
    validate_artifact_shape,
    validate_completion_artifacts,
)

REPORT_SECTIONS = artifact_validation._REPORT_SECTIONS


_EVIDENCE_WINDOWS = (
    "- Current alarm window: 2026-07-29T13:00:00Z ~ 2026-07-29T14:00:00Z\n"
    "- Historical comparison window: 2026-07-29T12:00:00Z ~ 2026-07-29T13:00:00Z\n"
)


def _report(*, omit: str | None = None) -> str:
    titles = [t for t in REPORT_SECTIONS if t != omit]
    body = {"증거 시간 범위": _EVIDENCE_WINDOWS, "대응 플레이북": "step-1"}
    return "\n".join(f"## {title}\n{body.get(title, 'placeholder')}\n" for title in titles)


def _scoping() -> dict:
    return {
        "stage": "SCOPING",
        "alarm_name": "alarm",
        "impact_scope": "service",
        "severity": "high",
        "summary": "s",
        "output_summary": "o",
        "metric_snapshot": {},
    }


def _hypotheses() -> dict:
    return {
        "stage": "HYPOTHESIS_GENERATION",
        "tree_id": "tree-1",
        "summary": "s",
        "output_summary": "o",
        "hypotheses": [{"hypothesis_id": "h1"}],
    }


def _playbook() -> dict:
    artifact: dict = {field: "value" for field in artifact_validation._PLAYBOOK_STRING_FIELDS}
    artifact["stage"] = "PLAYBOOK"
    artifact["verification_status"] = "DRAFT"
    artifact.update({field: [] for field in artifact_validation._PLAYBOOK_LIST_FIELDS})
    artifact["execution_steps"] = [
        {
            "step_id": "step-1",
            "intent": "restore the connection pool",
            "action": "restart the healthcare service",
            "success_criteria": "DatabaseConnections returns below 30",
        }
    ]
    return artifact


@pytest.mark.parametrize(
    ("filename", "builder"),
    [
        ("scoping.json", _scoping),
        ("hypotheses.json", _hypotheses),
        ("playbook.json", _playbook),
    ],
)
def test_minimal_valid_artifacts_pass_the_shape_check(filename, builder) -> None:
    validate_artifact_shape(filename, json.dumps(builder()))


def test_minimal_valid_report_passes_the_shape_check() -> None:
    validate_artifact_shape("report.md", _report())


@pytest.mark.parametrize("missing", artifact_validation._PLAYBOOK_STRING_FIELDS)
def test_every_playbook_string_field_the_gate_requires_is_checked_at_save(missing) -> None:
    artifact = _playbook()
    del artifact[missing]

    with pytest.raises(ArtifactValidationError, match=missing):
        validate_artifact_shape("playbook.json", json.dumps(artifact))


@pytest.mark.parametrize("missing", artifact_validation._PLAYBOOK_LIST_FIELDS)
def test_every_playbook_list_field_the_gate_requires_is_checked_at_save(missing) -> None:
    artifact = _playbook()
    del artifact[missing]

    with pytest.raises(ArtifactValidationError, match=missing):
        validate_artifact_shape("playbook.json", json.dumps(artifact))


@pytest.mark.parametrize("missing", REPORT_SECTIONS)
def test_every_report_section_the_gate_requires_is_checked_at_save(missing) -> None:
    with pytest.raises(ArtifactValidationError, match="report.md"):
        validate_artifact_shape("report.md", _report(omit=missing))


@pytest.mark.parametrize("filename", ["scoping.json", "hypotheses.json", "playbook.json"])
def test_malformed_json_is_rejected(filename) -> None:
    with pytest.raises(ArtifactValidationError, match="not valid JSON"):
        validate_artifact_shape(filename, "{not json")


@pytest.mark.parametrize("filename", ["scoping.json", "hypotheses.json", "playbook.json"])
@pytest.mark.parametrize("content", ["[]", '"text"', "42", "null"])
def test_non_object_json_is_rejected(filename, content) -> None:
    with pytest.raises(ArtifactValidationError, match="must be a JSON object"):
        validate_artifact_shape(filename, content)


def test_validation_artifacts_defer_to_the_completion_gate() -> None:
    # Their correctness depends on hypotheses the save call cannot see.
    validate_artifact_shape("validation-1.json", "{}")


def test_shape_check_does_not_demand_cross_artifact_agreement() -> None:
    # Whether the report's prose lists these steps is not knowable when the
    # playbook is saved, so agreement stays with the completion gate.
    artifact = _playbook()
    artifact["execution_steps"].append(
        {
            "step_id": "step-2",
            "intent": "verify",
            "action": "read the metric again",
            "success_criteria": "value stays below 30",
        }
    )

    validate_artifact_shape("playbook.json", json.dumps(artifact))


@pytest.mark.parametrize("missing", artifact_validation._EXECUTION_STEP_FIELDS)
def test_every_execution_step_field_the_gate_requires_is_checked_at_save(missing) -> None:
    artifact = _playbook()
    del artifact["execution_steps"][0][missing]

    with pytest.raises(ArtifactValidationError, match=missing):
        validate_artifact_shape("playbook.json", json.dumps(artifact))


def test_artifacts_that_pass_save_can_still_fail_the_completion_gate(tmp_path) -> None:
    # The two layers are not redundant: save catches self-contained shape errors,
    # the gate catches disagreement across artifacts.
    for filename, artifact in (
        ("scoping.json", _scoping()),
        ("hypotheses.json", _hypotheses()),
        ("playbook.json", _playbook()),
    ):
        content = json.dumps(artifact)
        validate_artifact_shape(filename, content)
        (tmp_path / filename).write_text(content)
    validate_artifact_shape("report.md", _report())
    (tmp_path / "report.md").write_text(_report())

    with pytest.raises(ArtifactValidationError):
        validate_completion_artifacts(tmp_path)
