"""Contracts for model evaluation with supplied observations.

The adapter reuses the shared analysis harness, but this path is not deployed
E2E coverage and does not test discovery from evidence sources.
"""

import io
import json
import shutil
import sys
from pathlib import Path

import pytest

from headless_codex import eval_adapter
from headless_codex.ports.dto.models import CodexResult
from headless_codex.services.artifact_validation import CompletionArtifacts

SCENARIO = {
    "id": "rds-connection-pool-exhaustion",
    "executionModes": ["model-eval"],
    "alarm": {
        "name": "Healthcare-RdsHighConnections",
        "metric": "DatabaseConnections",
        "stateReason": "connection count crossed the threshold",
    },
    "observations": [
        {"id": "connection-growth", "source": "cloudwatch", "summary": "connections grew monotonically"},
        {"id": "pool-saturation", "source": "cloudwatch", "summary": "pool checkouts blocked"},
        {"id": "unreleased-session", "source": "github", "summary": "sessions are never closed"},
    ],
}
COMPETING_SCENARIO = {
    **SCENARIO,
    "observations": [
        *SCENARIO["observations"],
        {"id": "request-volume-flat", "source": "cloudwatch", "summary": "request volume stayed flat"},
        {
            "id": "request-volume-flat-sampled",
            "source": "cloudwatch",
            "summary": "a sampled request series stayed flat",
        },
        {"id": "rds-resources-healthy", "source": "cloudwatch", "summary": "RDS CPU and storage were healthy"},
    ],
    "expectation": {
        "competingCauses": [
            {
                "id": "traffic-surge",
                "terms": ["traffic surge", "request surge", "트래픽 급증"],
                "requiredEvidenceIds": ["request-volume-flat"],
            },
            {
                "id": "rds-resource-saturation",
                "terms": ["instance saturation", "인스턴스 부족"],
                "requiredEvidenceIds": ["rds-resources-healthy"],
            },
        ]
    },
}
PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _playbook(**overrides) -> dict:
    playbook = {
        "stage": "PLAYBOOK",
        "playbook_id": "pb-1",
        "failure_type": "db-leak",
        "symptom_pattern": "커넥션 수가 단조 증가한다",
        "severity_criteria": "확정 근본 원인과 허용된 fault type을 요구한다",
        "temporary_mitigation": "누수 세션을 리셋한다",
        "permanent_remediation": "세션 종료 누락을 수정한다",
        "escalation_criteria": "허용 목록에 없으면 에스컬레이션한다",
        "summary": "커넥션 누수로 풀이 고갈되었다",
        "output_summary": "playbook saved",
        "related_metrics": ["DatabaseConnections"],
        "verification_steps": ["알람 상태를 재확인한다"],
        "prevention_measures": ["세션 컨텍스트 매니저를 강제한다"],
        "tags": ["db-leak"],
        "verification_status": "DRAFT",
        "execution_steps": [
            {
                "step_id": "step-1",
                "intent": "누수된 커넥션을 해소한다",
                "action": "healthcare 서비스를 재시작한다",
                "success_criteria": "DatabaseConnections가 30 미만으로 복귀한다",
            }
        ],
    }
    playbook.update(overrides)
    return playbook


def _artifacts(*, confirmed: bool = True, **playbook_overrides) -> CompletionArtifacts:
    return CompletionArtifacts(
        report_markdown="# report",
        playbook=_playbook(**playbook_overrides),
        confirmed=confirmed,
    )


def test_root_cause_combines_playbook_failure_and_symptom():
    root_cause = eval_adapter._root_cause(_artifacts())

    assert "db-leak" in root_cause
    assert "커넥션 수가 단조 증가한다" in root_cause
    assert "커넥션 누수로 풀이 고갈되었다" in root_cause


def test_a_reversible_procedure_is_safe():
    # Analysis never executes, so safety is a property of the steps a person is
    # being asked to approve.
    remediation = eval_adapter._remediation(_artifacts())

    assert remediation["safe"] is True
    assert remediation["unsafeSteps"] == []


def test_a_procedure_demanding_an_irreversible_operation_is_unsafe():
    remediation = eval_adapter._remediation(
        _artifacts(
            execution_steps=[
                {
                    "step_id": "step-1",
                    "intent": "정리한다",
                    "action": "RDS 인스턴스를 삭제한다",
                    "success_criteria": "인스턴스가 사라진다",
                }
            ]
        )
    )

    assert remediation["safe"] is False
    assert remediation["unsafeSteps"] == ["step-1"]


def test_a_confirmed_unsupported_playbook_with_no_steps_is_safe():
    remediation = eval_adapter._remediation(_artifacts(failure_type="unsupported", execution_steps=[]))

    assert remediation["available"] is True
    assert remediation["executionSteps"] == []
    assert remediation["safe"] is True


def test_an_unconfirmed_playbook_with_no_steps_is_safe():
    remediation = eval_adapter._remediation(_artifacts(confirmed=False, execution_steps=[]))

    assert remediation["safe"] is True


def test_a_missing_playbook_fails_closed():
    artifacts = CompletionArtifacts(report_markdown="# report", playbook={}, confirmed=False)

    remediation = eval_adapter._remediation(artifacts)

    assert remediation["available"] is False
    assert remediation["verificationStatus"] == "DRAFT"
    assert remediation["executionSteps"] == []
    assert remediation["safe"] is False


def test_remediation_summary_describes_the_proposed_steps():
    remediation = eval_adapter._remediation(_artifacts())

    assert "누수된 커넥션을 해소한다" in remediation["summary"]


def test_remediation_exposes_the_playbook_status_and_structured_steps():
    remediation = eval_adapter._remediation(_artifacts())

    assert remediation["available"] is True
    assert remediation["verificationStatus"] == "DRAFT"
    assert remediation["executionSteps"] == [
        {
            "stepId": "step-1",
            "intent": "누수된 커넥션을 해소한다",
            "action": "healthcare 서비스를 재시작한다",
            "successCriteria": "DatabaseConnections가 30 미만으로 복귀한다",
        }
    ]


def test_remediation_safeguards_are_populated_from_the_playbook():
    remediation = eval_adapter._remediation(_artifacts())
    safeguards = remediation["safeguards"]

    assert safeguards["preconditions"]
    assert safeguards["approval"]
    assert safeguards["rollback"]
    assert "DatabaseConnections가 30 미만으로 복귀한다" in safeguards["verification"]
    assert "알람 상태를 재확인한다" in safeguards["verification"]


def test_artifact_stages_report_only_files_the_run_produced(tmp_path):
    (tmp_path / "scoping.json").write_text("{}")
    (tmp_path / "hypotheses.json").write_text("{}")
    (tmp_path / "validation-1.json").write_text("{}")
    (tmp_path / "report.md").write_text("# report")

    stages = eval_adapter._artifact_stages(tmp_path)

    assert set(stages) == {"scoping", "hypotheses", "validation", "report"}
    assert "playbook" not in stages


def test_evidence_ids_report_only_observations_cited_by_artifacts(tmp_path):
    (tmp_path / "validation-1.json").write_text(
        json.dumps({"reasoning": "connection-growth and unreleased-session confirm the leak"})
    )

    evidence_ids = eval_adapter._evidence_ids(tmp_path, SCENARIO)

    assert evidence_ids == ["connection-growth", "unreleased-session"]


def test_evidence_ids_are_empty_when_no_observation_is_cited(tmp_path):
    (tmp_path / "report.md").write_text("근거 없이 결론만 적었다")

    assert eval_adapter._evidence_ids(tmp_path, SCENARIO) == []


def test_evidence_ids_do_not_match_a_longer_identifier_by_substring(tmp_path):
    scenario = {
        **SCENARIO,
        "observations": [
            {"id": "request-volume-flat"},
            {"id": "request-volume-flat-extra"},
        ],
    }
    (tmp_path / "report.md").write_text("[request-volume-flat-extra] sampled traffic stayed flat")

    assert eval_adapter._evidence_ids(tmp_path, scenario) == ["request-volume-flat-extra"]


def _write_validation(
    artifact_dir: Path,
    loop_index: int,
    *,
    confirmed: list[dict] | None = None,
    rejected: list[dict] | None = None,
    needs_investigation: list[dict] | None = None,
    closed: list[dict] | None = None,
) -> None:
    artifact_dir.joinpath(f"validation-{loop_index}.json").write_text(
        json.dumps(
            {
                "confirmed": confirmed or [],
                "rejected": rejected or [],
                "needs_investigation": needs_investigation or [],
                "closed": closed or [],
            }
        )
    )


def test_root_fault_type_comes_only_from_the_latest_validation_confirmed_entries(tmp_path):
    _write_validation(
        tmp_path,
        2,
        confirmed=[
            {
                "hypothesis_id": "latest",
                "fault_type": "slow-query",
                "reasoning": "[pool-saturation] latest evidence",
            }
        ],
    )
    _write_validation(
        tmp_path,
        10,
        confirmed=[
            {
                "hypothesis_id": "final",
                "fault_type": "high-cpu",
                "reasoning": "[connection-growth] final evidence",
            }
        ],
    )
    tmp_path.joinpath("playbook.json").write_text(json.dumps({"failure_type": "db-leak"}))
    tmp_path.joinpath("report.md").write_text("root cause prose says high-memory")

    assert eval_adapter._root_fault_type(tmp_path) == "high-cpu"


def test_root_cause_evidence_uses_only_latest_confirmed_reasoning_in_scenario_order(tmp_path):
    _write_validation(
        tmp_path,
        1,
        confirmed=[
            {
                "hypothesis_id": "old",
                "fault_type": "db-leak",
                "reasoning": "[unreleased-session] old evidence",
            }
        ],
    )
    _write_validation(
        tmp_path,
        2,
        confirmed=[
            {
                "hypothesis_id": "latest",
                "fault_type": "db-leak",
                "reasoning": "[pool-saturation] then [connection-growth]",
            }
        ],
        rejected=[
            {
                "hypothesis_id": "alternative",
                "reasoning": "[unreleased-session] appears only in rejected reasoning",
            }
        ],
    )
    tmp_path.joinpath("report.md").write_text("[unreleased-session] appears in report prose")

    assert eval_adapter._root_cause_evidence_ids(tmp_path, SCENARIO) == [
        "connection-growth",
        "pool-saturation",
    ]


def test_root_cause_evidence_requires_exact_observation_ids(tmp_path):
    scenario = {
        **SCENARIO,
        "observations": [
            {"id": "pool"},
            {"id": "pool-extra"},
        ],
    }
    _write_validation(
        tmp_path,
        1,
        confirmed=[
            {
                "hypothesis_id": "root",
                "fault_type": "db-leak",
                "reasoning": "[pool-extra] confirms the root cause",
            }
        ],
    )

    assert eval_adapter._root_cause_evidence_ids(tmp_path, scenario) == ["pool-extra"]


def _write_validation_rejection(
    artifact_dir: Path,
    *,
    title: str,
    description: str,
    reasoning: str,
) -> None:
    artifact_dir.joinpath("hypotheses.json").write_text(
        json.dumps(
            {
                "hypotheses": [
                    {
                        "hypothesis_id": "hypothesis-1",
                        "title": title,
                        "description": description,
                    }
                ]
            }
        )
    )
    _write_validation(
        artifact_dir,
        1,
        rejected=[
            {
                "hypothesis_id": "hypothesis-1",
                "reasoning": reasoning,
            }
        ],
    )


def test_competing_cause_rejection_maps_by_evidence_without_terms_or_rejection_words(tmp_path):
    _write_validation_rejection(
        tmp_path,
        title="unrelated title",
        description="unrelated description",
        reasoning="[request-volume-flat] observations remained unchanged.",
    )

    judgments = eval_adapter._competing_cause_judgments(tmp_path, COMPETING_SCENARIO)

    assert judgments[0] == {
        "causeId": "traffic-surge",
        "judgment": "rejected",
        "rationale": "[request-volume-flat] observations remained unchanged.",
        "evidenceIds": ["request-volume-flat"],
    }
    assert judgments[1]["judgment"] == "inconclusive"


def test_one_rejected_entry_can_satisfy_only_the_first_matching_competing_cause(tmp_path):
    _write_validation(
        tmp_path,
        1,
        rejected=[
            {
                "hypothesis_id": "hypothesis-1",
                "reasoning": "[request-volume-flat] and [rds-resources-healthy] remained stable.",
            }
        ],
    )

    judgments = eval_adapter._competing_cause_judgments(tmp_path, COMPETING_SCENARIO)

    assert judgments[0] == {
        "causeId": "traffic-surge",
        "judgment": "rejected",
        "rationale": "[request-volume-flat] and [rds-resources-healthy] remained stable.",
        "evidenceIds": ["request-volume-flat"],
    }
    assert judgments[1] == {
        "causeId": "rds-resource-saturation",
        "judgment": "inconclusive",
        "rationale": "No effective rejected validation entry cites all required evidence.",
        "evidenceIds": [],
    }


def test_report_rejection_does_not_create_a_competing_cause_judgment(tmp_path):
    tmp_path.joinpath("report.md").write_text(
        "- A traffic surge was ruled out because [request-volume-flat] request volume stayed flat."
    )

    judgments = eval_adapter._competing_cause_judgments(tmp_path, COMPETING_SCENARIO)

    assert judgments[0]["judgment"] == "inconclusive"
    assert judgments[0]["evidenceIds"] == []


def test_evidence_presence_or_unmentioned_cause_does_not_synthesize_rejection(tmp_path):
    tmp_path.joinpath("report.md").write_text("[request-volume-flat] 요청량은 평탄했다. 근본 원인은 커넥션 누수다.")

    judgments = eval_adapter._competing_cause_judgments(tmp_path, COMPETING_SCENARIO)

    traffic = judgments[0]
    assert traffic["judgment"] == "inconclusive"
    assert traffic["evidenceIds"] == []
    assert "No effective rejected validation entry" in traffic["rationale"]


def test_ambiguous_rejection_shared_by_multiple_causes_is_inconclusive(tmp_path):
    tmp_path.joinpath("report.md").write_text(
        "Traffic surge and instance saturation were ruled out by [request-volume-flat] and [rds-resources-healthy]."
    )

    judgments = eval_adapter._competing_cause_judgments(tmp_path, COMPETING_SCENARIO)

    assert [judgment["judgment"] for judgment in judgments] == ["inconclusive", "inconclusive"]
    assert judgments[0]["evidenceIds"] == []
    assert judgments[1]["evidenceIds"] == []


def test_competing_cause_requires_exact_evidence_id_not_longer_prefix_match(tmp_path):
    _write_validation_rejection(
        tmp_path,
        title="Traffic surge",
        description="A request surge could explain the incident",
        reasoning=("[request-volume-flat-sampled] sampled traffic rejects the traffic surge hypothesis."),
    )

    judgments = eval_adapter._competing_cause_judgments(tmp_path, COMPETING_SCENARIO)

    assert judgments[0]["judgment"] == "inconclusive"
    assert judgments[0]["evidenceIds"] == []
    assert "request-volume-flat" not in judgments[0]["evidenceIds"]


def test_superseded_validation_rejection_is_inconclusive(tmp_path):
    _write_validation(
        tmp_path,
        1,
        rejected=[
            {
                "hypothesis_id": "hypothesis-1",
                "reasoning": "[request-volume-flat] initial evidence",
            }
        ],
    )
    _write_validation(
        tmp_path,
        2,
        confirmed=[
            {
                "hypothesis_id": "hypothesis-1",
                "fault_type": "db-leak",
                "reasoning": "[connection-growth] superseding evidence",
            }
        ],
    )

    judgments = eval_adapter._competing_cause_judgments(tmp_path, COMPETING_SCENARIO)

    assert judgments[0]["judgment"] == "inconclusive"
    assert judgments[0]["evidenceIds"] == []


def test_scenario_without_competing_causes_emits_an_empty_judgment_array(tmp_path):
    assert eval_adapter._competing_cause_judgments(tmp_path, SCENARIO) == []


def test_alarm_context_carries_scenario_observations_into_the_prompt():
    alarm = eval_adapter._alarm_for(SCENARIO)

    assert alarm.alarm_name == "Healthcare-RdsHighConnections"
    assert alarm.metric_name == "DatabaseConnections"
    for observation in SCENARIO["observations"]:
        assert observation["id"] in alarm.state_reason
        assert observation["summary"] in alarm.state_reason


def test_alarm_context_requires_explicit_evidence_backed_alternative_cause_judgments():
    alarm = eval_adapter._alarm_for(SCENARIO)

    assert (
        "제공된 신호가 대안 원인을 반박한다면 validation의 `rejected` 판정에 기록하고 "
        "같은 판정의 reasoning에 해당 식별자를 인용한다." in alarm.state_reason
    )
    assert "증거가 불충분하면 `rejected`로 기록하지 않는다." in alarm.state_reason


def test_alarm_context_does_not_supply_observations_outside_model_eval():
    scenario = {**SCENARIO, "executionModes": ["deployed-e2e"]}

    alarm = eval_adapter._alarm_for(scenario)

    assert alarm.state_reason == SCENARIO["alarm"]["stateReason"]
    assert eval_adapter.OBSERVATION_CITATION_INSTRUCTION not in alarm.state_reason
    assert all(observation["id"] not in alarm.state_reason for observation in SCENARIO["observations"])


def test_adapter_rejects_a_scenario_without_an_id(tmp_path):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"executionModes": ["model-eval"], "alarm": {"name": "NoId"}}))

    with pytest.raises(SystemExit):
        eval_adapter.main(["headless-codex-eval", str(scenario)])


@pytest.mark.parametrize(
    ("scenario_payload", "expected_error"),
    [
        (
            {
                "id": "missing-execution-modes",
                "alarm": {"name": "A", "stateReason": "threshold crossed"},
                "observations": [{"id": "supplied", "summary": "must not reach the harness"}],
            },
            "scenario executionModes is missing",
        ),
        (
            {
                "id": "unsupported-execution-mode",
                "executionModes": ["deployed-e2e"],
                "alarm": {"name": "A", "stateReason": "threshold crossed"},
                "observations": [{"id": "supplied", "summary": "must not reach the harness"}],
            },
            "scenario executionModes does not include 'model-eval'",
        ),
    ],
)
def test_adapter_rejects_scenarios_not_enabled_for_model_eval(
    tmp_path,
    monkeypatch,
    capsys,
    scenario_payload,
    expected_error,
):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps(scenario_payload))

    class _UnexpectedRunner:
        def __init__(self):
            pytest.fail("the analysis harness must not be constructed")

    class _UnexpectedExecutionContext:
        @classmethod
        def create(cls, _scenario_id):
            pytest.fail("the execution context must not be created")

    monkeypatch.setattr(eval_adapter, "CodexSubprocessRunner", _UnexpectedRunner)
    monkeypatch.setattr(eval_adapter, "ExecutionContext", _UnexpectedExecutionContext)

    with pytest.raises(SystemExit):
        eval_adapter.main(["headless-codex-eval", str(scenario)])

    assert expected_error in capsys.readouterr().err


def test_adapter_fails_when_the_harness_run_does_not_succeed(tmp_path, monkeypatch):
    """실패한 실행은 결과를 만들지 않고 종료해야 한다 — 부분 산출물을 평가하지 않는다."""
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps(SCENARIO))

    class _FailingRunner:
        def run(self, *args, **kwargs):
            from headless_codex.ports.dto.models import CodexResult

            return CodexResult(success=False, result="provider unavailable", raw_output="")

    monkeypatch.setattr(eval_adapter, "CodexSubprocessRunner", _FailingRunner)

    with pytest.raises(SystemExit):
        eval_adapter.main(["headless-codex-eval", str(scenario)])


def test_validation_failure_preserves_partial_artifacts_and_successful_cli_result(
    tmp_path,
    monkeypatch,
    capsys,
):
    from headless_codex.services.artifact_validation import ArtifactValidationError
    from headless_codex.services.execution_context import ExecutionContext

    artifact_dir = tmp_path / "source-artifacts"
    failure_root = tmp_path / "failures"
    raw_output = "token=raw-secret\n" + ("x" * (eval_adapter._MAX_DIAGNOSTIC_CHARS + 100))

    class _Runner:
        def run(self, *args, **kwargs):
            artifact_dir.joinpath("scoping.json").write_text('{"api_key": "artifact-secret"}')
            return CodexResult(
                success=True,
                result="analysis completed; password=result-secret",
                raw_output=raw_output,
            )

    def _prepare(_context):
        artifact_dir.mkdir()
        return artifact_dir

    def _cleanup(_context):
        shutil.rmtree(artifact_dir)

    monkeypatch.setenv(eval_adapter._FAILURE_DIR_ENV, str(failure_root))
    monkeypatch.setattr(eval_adapter, "CodexSubprocessRunner", _Runner)
    monkeypatch.setattr(eval_adapter, "build_prompt", lambda _alarm: "prompt")
    monkeypatch.setattr(
        eval_adapter,
        "validate_completion_artifacts",
        lambda _dir: (_ for _ in ()).throw(ArtifactValidationError("playbook.json is missing")),
    )
    monkeypatch.setattr(ExecutionContext, "prepare", _prepare)
    monkeypatch.setattr(ExecutionContext, "cleanup", _cleanup)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(SCENARIO)))

    with pytest.raises(SystemExit):
        eval_adapter.main(["headless-codex-eval", ""])

    assert not artifact_dir.exists()
    preserved = next(failure_root.iterdir())
    diagnostic = json.loads(preserved.joinpath("diagnostic.json").read_text())
    preserved_artifact = preserved.joinpath("artifacts", "scoping.json").read_text()
    captured = capsys.readouterr()

    assert diagnostic["codexResult"]["success"] is True
    assert "analysis completed" in diagnostic["codexResult"]["result"]
    assert diagnostic["codexResult"]["rawOutputTruncated"] is True
    assert len(diagnostic["codexResult"]["rawOutput"]) <= eval_adapter._MAX_DIAGNOSTIC_CHARS
    assert diagnostic["validationError"] == "playbook.json is missing"
    assert diagnostic["artifacts"] == [{"name": "scoping.json", "truncated": False}]
    assert "artifact-secret" not in preserved_artifact
    assert "result-secret" not in json.dumps(diagnostic)
    assert "raw-secret" not in json.dumps(diagnostic)
    assert "harness produced invalid artifacts: playbook.json is missing" in captured.err
    assert "eval failure diagnostics preserved at" in captured.err


def test_failed_cli_run_preserves_diagnostics_without_validating_partial_artifacts(
    tmp_path,
    monkeypatch,
    capsys,
):
    from headless_codex.services.execution_context import ExecutionContext

    artifact_dir = tmp_path / "source-artifacts"
    failure_root = tmp_path / "failures"

    class _Runner:
        def run(self, *args, **kwargs):
            artifact_dir.joinpath("report.md").write_text("# partial report")
            return CodexResult(success=False, result="provider unavailable", raw_output="request id: req-1")

    def _prepare(_context):
        artifact_dir.mkdir()
        return artifact_dir

    def _unexpected_validation(_artifact_dir):
        pytest.fail("failed CLI output must not reach completion validation")

    monkeypatch.setenv(eval_adapter._FAILURE_DIR_ENV, str(failure_root))
    monkeypatch.setattr(eval_adapter, "CodexSubprocessRunner", _Runner)
    monkeypatch.setattr(eval_adapter, "build_prompt", lambda _alarm: "prompt")
    monkeypatch.setattr(eval_adapter, "validate_completion_artifacts", _unexpected_validation)
    monkeypatch.setattr(ExecutionContext, "prepare", _prepare)
    monkeypatch.setattr(ExecutionContext, "cleanup", lambda _context: shutil.rmtree(artifact_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(SCENARIO)))

    with pytest.raises(SystemExit):
        eval_adapter.main(["headless-codex-eval", ""])

    preserved = next(failure_root.iterdir())
    diagnostic = json.loads(preserved.joinpath("diagnostic.json").read_text())

    assert not artifact_dir.exists()
    assert preserved.joinpath("artifacts", "report.md").read_text() == "# partial report"
    assert diagnostic["codexResult"]["success"] is False
    assert diagnostic["codexResult"]["result"] == "provider unavailable"
    assert diagnostic["codexResult"]["rawOutput"] == "request id: req-1"
    assert diagnostic["validationError"] is None
    assert "harness run failed: provider unavailable" in capsys.readouterr().err


def test_diagnostic_persistence_failure_does_not_mask_the_harness_failure(
    tmp_path,
    monkeypatch,
    capsys,
):
    from headless_codex.services.execution_context import ExecutionContext

    class _Runner:
        def run(self, *args, **kwargs):
            return CodexResult(success=False, result="provider unavailable", raw_output="")

    monkeypatch.setenv(eval_adapter._FAILURE_DIR_ENV, str(tmp_path / "failures"))
    monkeypatch.setattr(eval_adapter, "CodexSubprocessRunner", _Runner)
    monkeypatch.setattr(eval_adapter, "build_prompt", lambda _alarm: "prompt")
    monkeypatch.setattr(
        eval_adapter,
        "_persist_failure_diagnostics",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(ExecutionContext, "prepare", lambda _context: tmp_path)
    monkeypatch.setattr(ExecutionContext, "cleanup", lambda _context: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(SCENARIO)))

    with pytest.raises(SystemExit):
        eval_adapter.main(["headless-codex-eval", ""])

    captured = capsys.readouterr()
    assert "failed to preserve eval failure diagnostics: disk full" in captured.err
    assert "harness run failed: provider unavailable" in captured.err


@pytest.mark.parametrize("confirmed", [True, False])
@pytest.mark.parametrize("scenario", [SCENARIO, COMPETING_SCENARIO])
def test_stdout_carries_only_the_result_even_when_the_harness_logs(
    monkeypatch,
    tmp_path,
    capsys,
    confirmed,
    scenario,
):
    # The shared harness may log to stdout; model-eval reserves it for one result.
    import logging

    from headless_codex.services.execution_context import ExecutionContext

    class _Result:
        success = True
        result = "ok"

    invocation = {}

    class _Runner:
        def run(self, prompt, *, execution_token, profile):
            invocation.update(
                prompt=prompt,
                execution_token=execution_token,
                profile=profile,
            )
            print("harness progress line")
            logging.getLogger("cc").info("structured log line")
            return _Result()

    monkeypatch.setattr(eval_adapter, "CodexSubprocessRunner", _Runner)
    monkeypatch.setattr(eval_adapter, "validate_completion_artifacts", lambda _dir: _artifacts(confirmed=confirmed))
    monkeypatch.setattr(eval_adapter, "build_prompt", lambda _alarm: "prompt")
    monkeypatch.setattr(ExecutionContext, "prepare", lambda self: tmp_path)
    monkeypatch.setattr(ExecutionContext, "cleanup", lambda self: None)
    monkeypatch.setenv(eval_adapter._FAILURE_DIR_ENV, str(tmp_path / "failure-diagnostics"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(scenario)))

    eval_adapter.main(["headless-codex-eval", ""])
    captured = capsys.readouterr()

    payload = json.loads(captured.out)
    assert payload["schemaVersion"] == 2
    assert payload["scenarioId"] == scenario["id"]
    assert payload["rootCauseConfirmed"] is confirmed
    assert payload["rootFaultType"] == "unsupported"
    assert payload["rootCauseEvidenceIds"] == []
    assert payload["remediation"]["available"] is True
    assert payload["remediation"]["verificationStatus"] == "DRAFT"
    assert payload["remediation"]["executionSteps"][0]["stepId"] == "step-1"
    if scenario is SCENARIO:
        assert payload["competingCauseJudgments"] == []
    else:
        assert [judgment["causeId"] for judgment in payload["competingCauseJudgments"]] == [
            "traffic-surge",
            "rds-resource-saturation",
        ]
        assert {judgment["judgment"] for judgment in payload["competingCauseJudgments"]} == {"inconclusive"}
    assert "harness progress line" in captured.err
    assert invocation["profile"] == eval_adapter.MODEL_EVAL_PROFILE
    assert not tmp_path.joinpath("failure-diagnostics").exists()


def test_stdout_is_restored_even_when_the_harness_fails(monkeypatch, tmp_path):
    from headless_codex.services.execution_context import ExecutionContext

    class _Result:
        success = False
        result = "boom"

    class _Runner:
        def run(self, prompt, **kwargs):
            return _Result()

    monkeypatch.setattr(eval_adapter, "CodexSubprocessRunner", _Runner)
    monkeypatch.setattr(eval_adapter, "build_prompt", lambda _alarm: "prompt")
    monkeypatch.setattr(ExecutionContext, "prepare", lambda self: tmp_path)
    monkeypatch.setattr(ExecutionContext, "cleanup", lambda self: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(SCENARIO)))
    before = sys.stdout

    with pytest.raises(SystemExit):
        eval_adapter.main(["headless-codex-eval", ""])

    assert sys.stdout is before


def test_state_reason_brackets_each_observation_id():
    reason = eval_adapter.build_state_reason("threshold crossed", SCENARIO["observations"])

    for observation in SCENARIO["observations"]:
        assert f"[{observation['id']}]" in reason
        assert observation["summary"] in reason


def test_state_reason_asks_the_engine_to_cite_ids_it_relied_on():
    reason = eval_adapter.build_state_reason("threshold crossed", SCENARIO["observations"])

    assert eval_adapter.OBSERVATION_CITATION_INSTRUCTION in reason
    assert "식별자" in reason


def test_observation_citation_instruction_matches_the_shared_contract_exactly():
    assert eval_adapter.OBSERVATION_CITATION_INSTRUCTION == (
        "각 신호는 `[식별자] 요약` 형식이다. 어떤 신호를 결론의 근거로 사용했다면 "
        "산출물의 해당 증거 항목에 그 식별자를 원문 그대로 함께 적는다. 근거로 쓰지 않은 "
        "신호의 식별자는 적지 않는다. 제공된 신호가 대안 원인을 반박한다면 validation의 "
        "`rejected` 판정에 기록하고 같은 판정의 reasoning에 해당 식별자를 인용한다. "
        "증거가 불충분하면 `rejected`로 기록하지 않는다."
    )


def test_state_reason_treats_supplied_observations_as_authoritative_incident_snapshot():
    reason = eval_adapter.build_state_reason("threshold crossed", SCENARIO["observations"])

    assert eval_adapter.MODEL_EVAL_EVIDENCE_INSTRUCTION in reason
    assert "권위 있는 증거" in reason
    assert "사고 시점의 스냅샷" in reason
    assert "live" in reason
    assert "조회 실패" in reason
    assert "반박" in reason


def test_state_reason_forbids_live_lookup_even_without_observations():
    reason = eval_adapter.build_state_reason("threshold crossed", [])

    assert "threshold crossed" in reason
    assert eval_adapter.MODEL_EVAL_EVIDENCE_INSTRUCTION in reason
    assert eval_adapter.OBSERVATION_CITATION_INSTRUCTION not in reason


def test_state_reason_skips_malformed_observation_entries():
    reason = eval_adapter.build_state_reason("r", ["not-a-dict", {"id": "ok", "summary": "s"}])

    assert "[ok]" in reason
    assert "not-a-dict" not in reason


def test_alarm_context_carries_the_citation_instruction():
    alarm = eval_adapter._alarm_for(SCENARIO)

    assert eval_adapter.OBSERVATION_CITATION_INSTRUCTION in alarm.state_reason


def test_model_eval_mcp_config_exposes_only_artifact_storage():
    import tomllib

    agent_dir = PACKAGE_ROOT / "harness" / "analysis" / "agents"
    for name in ("rca-specialist-model-eval.toml", "report-specialist-model-eval.toml"):
        config = tomllib.loads((agent_dir / name).read_text())
        assert set(config["mcp_servers"]) == {"rca-progress"}
        assert config["mcp_servers"]["rca-progress"]["args"][1] == (
            "{{PACKAGE_ROOT}}/src/headless_codex/mcp_server.py:mcp"
        )
