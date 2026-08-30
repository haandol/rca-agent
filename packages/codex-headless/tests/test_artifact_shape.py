"""Save-time shape checks must agree with the completion gate.

The save call runs while the agent can still fix its output; the completion gate
runs after the run has ended. If the two disagree about a field the artifact owns,
a run can pass every save and still fail at the end with nothing recoverable —
which is the failure this check exists to prevent.

The skill the agent reads is the third layer. It can disagree with both while
every test passes, because nothing else reads it — so the run only breaks live.
"""

import json
from pathlib import Path

import pytest

from codex_headless.services import artifact_validation
from codex_headless.services.artifact_validation import (
    ArtifactValidationError,
    validate_artifact_shape,
    validate_completion_artifacts,
)

REPORT_SECTIONS = artifact_validation._REPORT_SECTIONS
SKILLS_DIR = Path(__file__).resolve().parents[1] / "harness" / "skills"


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
        "metric_observations": [
            {
                "metric_name": "DatabaseConnections",
                "datapoints": [2, 12, 20, 27, 30],
                "trend": "rising",
                "window_start": "2026-08-01T00:00:00Z",
                "window_end": "2026-08-01T00:30:00Z",
                "unit": "Count",
                "baseline": 2,
            }
        ],
        "concurrent_alarms": [{"alarm_name": "VitalIngestFailures", "state": "ALARM"}],
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


def test_the_validation_skill_names_every_list_the_gate_requires() -> None:
    """The skill is what the agent writes from, so a field it omits never gets written.

    A live run wrote `judgments[]` with a status field because the skill documented
    that shape, while the gate wanted five state-separated lists. Three runs
    finished every artifact and were then discarded at the gate, each naming a
    different missing list — the agent was guessing at a schema it had never been
    shown.
    """
    skill = (SKILLS_DIR / "hypothesis-validation" / "SKILL.md").read_text()

    for field in ("confirmed", "rejected", "needs_investigation", "closed", "new_hypotheses"):
        assert f'"{field}"' in skill, f"the validation skill never shows the {field} list"

    # The shape that broke it. Keeping it out is the point, not incidental.
    assert '"judgments"' not in skill


def test_the_generation_skill_names_every_field_the_gate_requires() -> None:
    skill = (SKILLS_DIR / "hypothesis-generation" / "SKILL.md").read_text()

    for field in ("stage", "tree_id", "hypotheses", "summary", "output_summary"):
        assert f'"{field}"' in skill, f"the generation skill never shows {field}"


def test_the_evidence_skill_shows_the_observation_shape() -> None:
    """관측 형태를 스킬이 보여주지 않으면 모델이 다시 두 숫자로 요약한다."""
    skill = (SKILLS_DIR / "evidence-patterns" / "SKILL.md").read_text()

    for field in ("metric_observations", "datapoints", "trend", "shape_note", "concurrent_alarms"):
        assert f'"{field}"' in skill, f"the evidence skill never shows {field}"


def test_an_observation_may_describe_a_shape_the_vocabulary_lacks() -> None:
    """다섯 항목이 관측의 표현력을 제한하지 않아야 한다.

    처음 보는 패턴을 가장 가까운 항목으로 뭉개면 그 패턴이 사라지고, 계약이 커버리지를
    좁히는 쪽으로 작동한다.
    """
    artifact = _scoping()
    artifact["metric_observations"][0]["shape_note"] = "계단식으로 두 번 올라 각각 유지됐다"

    validate_artifact_shape("scoping.json", json.dumps(artifact))


@pytest.mark.parametrize("missing", ["metric_observations", "concurrent_alarms"])
def test_scoping_must_carry_the_observation_arrays(missing) -> None:
    artifact = _scoping()
    del artifact[missing]

    with pytest.raises(ArtifactValidationError, match=missing):
        validate_artifact_shape("scoping.json", json.dumps(artifact))


def test_an_observation_cannot_claim_a_trend_it_did_not_observe() -> None:
    """두 점 미만으로 추세를 단정하는 것이 이 계약이 막으려는 실패다."""
    artifact = _scoping()
    artifact["metric_observations"][0]["datapoints"] = [30]

    with pytest.raises(ArtifactValidationError, match="datapoint"):
        validate_artifact_shape("scoping.json", json.dumps(artifact))


def test_an_observation_with_too_few_points_may_report_unknown() -> None:
    artifact = _scoping()
    artifact["metric_observations"][0]["datapoints"] = []
    artifact["metric_observations"][0]["trend"] = "unknown"

    validate_artifact_shape("scoping.json", json.dumps(artifact))


def test_an_observation_trend_outside_the_vocabulary_is_refused() -> None:
    artifact = _scoping()
    artifact["metric_observations"][0]["trend"] = "climbing"

    with pytest.raises(ArtifactValidationError, match="trend"):
        validate_artifact_shape("scoping.json", json.dumps(artifact))


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
