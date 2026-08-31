import json
import os
import uuid
from contextlib import suppress
from pathlib import Path

import pytest

from headless_codex import mcp_server
from headless_codex.services import artifact_validation, execution_context
from headless_codex.services.execution_context import (
    RUN_TOKEN_ENV,
    ExecutionContext,
)

_EVIDENCE_WINDOWS = (
    "- Current alarm window: 2026-07-29T13:00:00Z ~ 2026-07-29T14:00:00Z\n"
    "- Historical comparison window: 2026-07-29T12:00:00Z ~ 2026-07-29T13:00:00Z\n"
)


def _minimal_valid_artifact(filename: str) -> str:
    """Smallest content that passes the save-time shape check for each artifact."""
    if filename == "report.md":
        return "\n".join(
            f"## {title}\n{_EVIDENCE_WINDOWS if title == '증거 시간 범위' else 'placeholder'}\n"
            for title in artifact_validation._REPORT_SECTIONS
        )

    if filename == "scoping.json":
        return json.dumps(
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
            }
        )

    if filename == "hypotheses.json":
        return json.dumps(
            {
                "stage": "HYPOTHESIS_GENERATION",
                "tree_id": "tree-1",
                "summary": "s",
                "output_summary": "o",
                "hypotheses": [
                    {
                        "hypothesis_id": f"h{index}",
                        "tree_id": "tree-1",
                        "title": f"hypothesis {index}",
                        "description": f"description {index}",
                        "fault_type": "db-leak" if index == 1 else "unsupported",
                        "category": "INFRASTRUCTURE",
                        "confidence_score": 0.6,
                        "required_evidence": ["metric"],
                        "status": "PENDING",
                        "parent_id": None,
                        "depth": 0,
                    }
                    for index in range(1, 4)
                ],
            }
        )

    if filename == "playbook.json":
        artifact: dict = {field: "value" for field in artifact_validation._PLAYBOOK_STRING_FIELDS}
        artifact["stage"] = "PLAYBOOK"
        artifact["verification_status"] = "DRAFT"
        artifact.update({field: [] for field in artifact_validation._PLAYBOOK_LIST_FIELDS})
        return json.dumps(artifact)

    if filename == "validation-1.json":
        return json.dumps(
            {
                "stage": "VALIDATION",
                "loop_index": 1,
                "confirmed": [
                    {
                        "hypothesis_id": "h1",
                        "confidence": 0.95,
                        "fault_type": "db-leak",
                        "reasoning": "confirmed",
                        "evidence_summary": ["metric evidence"],
                        "evidence_collection_failed": False,
                    }
                ],
                "rejected": [],
                "needs_investigation": [],
                "closed": [],
                "new_hypotheses": [],
                "summary": "s",
                "output_summary": "o",
            }
        )
    return "{}"


@pytest.fixture
def artifact_home(monkeypatch, tmp_path):
    token = uuid.uuid4().hex
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path / "runs")
    monkeypatch.setenv(RUN_TOKEN_ENV, token)
    context = ExecutionContext(rca_id="rca-1", token=token)
    base = context.prepare()
    yield base
    context.cleanup()


def _save(filename: str, content: str) -> str:
    """Route through the tool whose role owns the filename.

    Existing cases assert the shape and path rules both tools share, so they call
    the owning tool rather than repeating themselves per role. The role boundary
    itself is asserted separately.
    """
    if filename in {"report.md", "playbook.json"}:
        return mcp_server.save_report_artifact(filename, content)
    return mcp_server.save_analysis_artifact(filename, content)


def _save_rejected(filename: str, content: str) -> bool:
    try:
        result = json.loads(_save(filename, content))
    except (OSError, ValueError):
        return True
    return result.get("ok") is False


def _save_completed_analysis() -> None:
    assert json.loads(_save("scoping.json", _minimal_valid_artifact("scoping.json")))["ok"] is True
    assert json.loads(_save("hypotheses.json", _minimal_valid_artifact("hypotheses.json")))["ok"] is True
    result = json.loads(_save("validation-1.json", _minimal_valid_artifact("validation-1.json")))
    assert result["ok"] is True
    assert result["decision"]["action"] == "REPORT"


@pytest.mark.parametrize(
    "filename",
    [
        "../escaped.json",
        "../../escaped.json",
        "/tmp/escaped.json",
        "nested/report.md",
        r"..\escaped.json",
    ],
)
def test_save_artifact_rejects_path_traversal_and_nested_paths(artifact_home, filename):
    escaped = artifact_home.parent / "escaped.json"
    escaped.unlink(missing_ok=True)

    try:
        assert _save_rejected(filename, "{}") is True
        assert not escaped.exists()
    finally:
        escaped.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "filename",
    ["scoping.json", "hypotheses.json"],
)
def test_save_artifact_accepts_canonical_names(artifact_home, filename):
    content = _minimal_valid_artifact(filename)

    result = json.loads(_save(filename, content))

    assert result["ok"] is True
    assert Path(result["path"]) == artifact_home / filename
    saved = (artifact_home / filename).read_text()
    if filename == "scoping.json":
        assert saved == content
    else:
        artifact = json.loads(saved)
        assert artifact["generation_round"] == 1
        assert artifact["after_loop_index"] == 0


def test_save_artifact_accepts_validation_after_hypotheses(artifact_home):
    _save_completed_analysis()

    assert (artifact_home / "validation-1.json").is_file()


@pytest.mark.parametrize("filename", ["playbook.json", "report.md"])
def test_save_report_artifact_accepts_canonical_names_after_analysis_completion(artifact_home, filename):
    _save_completed_analysis()

    result = json.loads(_save(filename, _minimal_valid_artifact(filename)))

    assert result["ok"] is True
    assert Path(result["path"]) == artifact_home / filename


def test_validation_99_is_rejected_by_the_three_loop_server_limit(artifact_home):
    _save_completed_analysis()

    result = json.loads(_save("validation-99.json", "{}"))

    assert result["ok"] is False
    assert "maximum of 3" in result["error"]


class _StateStore:
    def __init__(self):
        self.state = "SCOPING"
        self.transitions: list[str] = []
        self.fail_target: str | None = None

    def get_state(self, _rca_id, *, claim_token):
        assert claim_token == "claim"
        return self.state

    def update_state(self, _rca_id, state, *, claim_token):
        assert claim_token == "claim"
        if state == self.fail_target:
            raise RuntimeError(f"failed at {state}")
        self.state = state
        self.transitions.append(state)


def test_artifact_saves_advance_the_shared_analysis_state_machine(artifact_home, monkeypatch):
    store = _StateStore()
    monkeypatch.setattr(
        mcp_server,
        "_runtime_session",
        lambda: (store, None, "rca-1", "claim"),
    )
    monkeypatch.setattr(mcp_server, "_persist_runtime_trace", lambda *_args: None)

    _save_completed_analysis()
    assert store.transitions == [
        "HYPOTHESIS_GENERATION",
        "HYPOTHESIS_PRIORITIZATION",
        "EVIDENCE_COLLECTION",
        "HYPOTHESIS_VALIDATION",
    ]

    result = json.loads(_save("playbook.json", _minimal_valid_artifact("playbook.json")))

    assert result["ok"] is True
    assert store.state == "REPORT_GENERATION"


def test_failed_state_transition_rolls_back_the_artifact_for_retry(artifact_home, monkeypatch):
    store = _StateStore()
    monkeypatch.setattr(
        mcp_server,
        "_runtime_session",
        lambda: (store, None, "rca-1", "claim"),
    )
    monkeypatch.setattr(mcp_server, "_persist_runtime_trace", lambda *_args: None)
    assert json.loads(_save("scoping.json", _minimal_valid_artifact("scoping.json")))["ok"] is True
    assert json.loads(_save("hypotheses.json", _minimal_valid_artifact("hypotheses.json")))["ok"] is True
    store.fail_target = "EVIDENCE_COLLECTION"

    failed = json.loads(_save("validation-1.json", _minimal_valid_artifact("validation-1.json")))

    assert failed["ok"] is False
    assert not (artifact_home / "validation-1.json").exists()

    store.fail_target = None
    retried = json.loads(_save("validation-1.json", _minimal_valid_artifact("validation-1.json")))

    assert retried["ok"] is True


def test_regeneration_decision_can_retry_after_trace_failure(artifact_home, monkeypatch):
    store = _StateStore()
    monkeypatch.setattr(
        mcp_server,
        "_runtime_session",
        lambda: (store, None, "rca-1", "claim"),
    )
    monkeypatch.setattr(mcp_server, "_persist_runtime_trace", lambda *_args: None)
    assert json.loads(_save("scoping.json", _minimal_valid_artifact("scoping.json")))["ok"] is True
    assert json.loads(_save("hypotheses.json", _minimal_valid_artifact("hypotheses.json")))["ok"] is True
    failures = iter([RuntimeError("trace unavailable"), None])

    def persist_with_one_failure(*_args):
        failure = next(failures)
        if failure:
            raise failure

    monkeypatch.setattr(mcp_server, "_persist_runtime_trace", persist_with_one_failure)
    validation = json.loads(_minimal_valid_artifact("validation-1.json"))
    validation["confirmed"] = []
    validation["rejected"] = [
        {
            "hypothesis_id": f"h{index}",
            "confidence": 0.1,
            "reasoning": "rejected",
            "evidence_summary": ["counter evidence"],
            "evidence_collection_failed": False,
        }
        for index in range(1, 4)
    ]

    failed = json.loads(_save("validation-1.json", json.dumps(validation)))

    assert failed["ok"] is False
    assert store.state == "HYPOTHESIS_GENERATION"
    assert not (artifact_home / "validation-1.json").exists()

    retried = json.loads(_save("validation-1.json", json.dumps(validation)))

    assert retried["ok"] is True
    assert retried["decision"]["action"] == "REGENERATE"


def test_regeneration_saves_a_new_round_without_overwriting_the_first(artifact_home):
    assert json.loads(_save("scoping.json", _minimal_valid_artifact("scoping.json")))["ok"] is True
    assert json.loads(_save("hypotheses.json", _minimal_valid_artifact("hypotheses.json")))["ok"] is True
    validation = json.loads(_minimal_valid_artifact("validation-1.json"))
    validation["confirmed"] = []
    validation["rejected"] = [
        {
            "hypothesis_id": f"h{index}",
            "confidence": 0.1,
            "reasoning": "rejected",
            "evidence_summary": ["counter evidence"],
            "evidence_collection_failed": False,
        }
        for index in range(1, 4)
    ]
    decision = json.loads(_save("validation-1.json", json.dumps(validation)))
    assert decision["decision"]["action"] == "REGENERATE"
    second_round = json.loads(_minimal_valid_artifact("hypotheses.json"))
    second_round["tree_id"] = "tree-2"
    for index, hypothesis in enumerate(second_round["hypotheses"], start=1):
        hypothesis["hypothesis_id"] = f"round-2-{index}"
        hypothesis["tree_id"] = "tree-2"

    result = json.loads(_save("hypotheses-2.json", json.dumps(second_round)))

    assert result["ok"] is True
    assert (artifact_home / "hypotheses.json").is_file()
    saved_round = json.loads((artifact_home / "hypotheses-2.json").read_text())
    assert saved_round["generation_round"] == 2
    assert saved_round["after_loop_index"] == 1


@pytest.mark.parametrize(
    "filename",
    ["notes.txt", "validation-1.md", "validation-x.json", "report.json", "scoping.md", ".hidden.json"],
)
def test_save_artifact_rejects_unknown_names_and_extensions(artifact_home, filename):
    assert _save_rejected(filename, "content") is True
    assert not (artifact_home / filename).exists()


@pytest.mark.parametrize("missing", artifact_validation._PLAYBOOK_STRING_FIELDS)
def test_save_artifact_rejects_playbook_missing_a_required_field(artifact_home, missing):
    # A field omitted here used to be accepted and only surfaced at the
    # completion gate, after the run had ended and could no longer be corrected.
    artifact = json.loads(_minimal_valid_artifact("playbook.json"))
    del artifact[missing]

    result = json.loads(_save("playbook.json", json.dumps(artifact)))

    assert result["ok"] is False
    assert missing in result["error"]
    assert not (artifact_home / "playbook.json").exists()


def test_save_artifact_rejection_tells_the_agent_to_save_again(artifact_home):
    artifact = json.loads(_minimal_valid_artifact("playbook.json"))
    del artifact["severity_criteria"]

    result = json.loads(_save("playbook.json", json.dumps(artifact)))

    assert "save again" in result["error"]


@pytest.mark.parametrize(
    "filename",
    ["scoping.json", "hypotheses.json", "playbook.json"],
)
def test_save_artifact_rejects_malformed_json(artifact_home, filename):
    assert _save_rejected(filename, "{not json") is True
    assert not (artifact_home / filename).exists()


@pytest.mark.parametrize(
    "filename",
    ["scoping.json", "hypotheses.json", "playbook.json"],
)
def test_save_artifact_rejects_a_json_array(artifact_home, filename):
    assert _save_rejected(filename, "[]") is True
    assert not (artifact_home / filename).exists()


def test_save_artifact_rejects_report_missing_a_required_section(artifact_home):
    full = _minimal_valid_artifact("report.md")
    truncated = full.split("## Action Items")[0]

    assert _save_rejected("report.md", truncated) is True
    assert not (artifact_home / "report.md").exists()


def test_save_artifact_rejects_wrong_stage_value(artifact_home):
    artifact = json.loads(_minimal_valid_artifact("playbook.json"))
    artifact["stage"] = "REPORT"

    assert _save_rejected("playbook.json", json.dumps(artifact)) is True


def test_save_artifact_rejects_validation_without_hypotheses(artifact_home):
    result = json.loads(_save("validation-1.json", "{}"))

    assert result["ok"] is False
    assert "hypotheses.json is missing" in result["error"]


def test_model_cannot_submit_server_owned_validation_fields(artifact_home):
    assert json.loads(_save("hypotheses.json", _minimal_valid_artifact("hypotheses.json")))["ok"] is True
    validation = json.loads(_minimal_valid_artifact("validation-1.json"))
    validation["server_decision"] = {"action": "REPORT"}
    validation["confirmed"][0]["server_rejected"] = True

    result = json.loads(_save("validation-1.json", json.dumps(validation)))

    assert result["ok"] is False
    assert "server_decision is server-owned" in result["error"]


def test_save_artifact_preserves_existing_file_when_atomic_replace_fails(artifact_home, monkeypatch):
    target = artifact_home / "report.md"
    target.write_text("stable report")

    def _replace_failure(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _replace_failure)
    with suppress(OSError):
        _save("report.md", "new report")

    assert target.read_text() == "stable report"


@pytest.mark.parametrize("token", [None, "", "../escape", "g" * 32, "a" * 31, "a" * 33])
def test_save_artifact_rejects_missing_or_invalid_execution_token(monkeypatch, tmp_path, token):
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path / "runs")
    if token is None:
        monkeypatch.delenv(RUN_TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(RUN_TOKEN_ENV, token)

    result = json.loads(_save("report.md", "must not be written"))

    assert result["ok"] is False
    assert not (tmp_path / "runs").exists()


def test_save_artifact_rejects_valid_token_without_prepared_run_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path / "runs")
    monkeypatch.setenv(RUN_TOKEN_ENV, uuid.uuid4().hex)

    result = json.loads(_save("report.md", "must not be written"))

    assert result["ok"] is False


def test_save_artifact_rejects_symlinked_run_directory(monkeypatch, tmp_path):
    token = uuid.uuid4().hex
    artifact_root = tmp_path / "runs"
    outside = tmp_path / "outside"
    artifact_root.mkdir()
    outside.mkdir()
    artifact_root.joinpath(token).symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", artifact_root)
    monkeypatch.setenv(RUN_TOKEN_ENV, token)

    result = json.loads(_save("report.md", "must not be written"))

    assert result["ok"] is False
    assert not outside.joinpath("report.md").exists()


def test_analysis_server_exposes_no_tool_that_changes_a_service():
    # The analysis run is read-only. Recovery happens in a separate agent after a
    # person approves, so a write tool here would put execution back inside
    # analysis and bypass that approval.
    tools = [name for name in dir(mcp_server) if not name.startswith("_")]

    assert "execute_healthcare_reset" not in tools
    assert not any("reset" in name.lower() for name in tools)


def test_save_artifact_rejects_a_server_owned_remediation_result(artifact_home):
    # remediation.json belonged to the retired automated-recovery path. Nothing
    # writes it now, and the analysis agent must not resurrect it to claim a
    # recovery it never performed.
    assert _save_rejected("remediation.json", "{}") is True
    assert not (artifact_home / "remediation.json").exists()


def test_save_artifact_rejects_a_playbook_claiming_it_was_verified(artifact_home):
    # A playbook is a draft until an execution and its retrospective exercise it,
    # so analysis may not present one as verified.
    artifact = json.loads(_minimal_valid_artifact("playbook.json"))
    artifact["verification_status"] = "VERIFIED"

    result = json.loads(_save("playbook.json", json.dumps(artifact)))

    assert result["ok"] is False
    assert "DRAFT" in result["error"]


def test_save_artifact_rejects_execution_steps_missing_their_contract(artifact_home):
    for missing in artifact_validation._EXECUTION_STEP_FIELDS:
        artifact = json.loads(_minimal_valid_artifact("playbook.json"))
        step = {field: "value" for field in artifact_validation._EXECUTION_STEP_FIELDS}
        del step[missing]
        artifact["execution_steps"] = [step]

        result = json.loads(_save("playbook.json", json.dumps(artifact)))

        assert result["ok"] is False, missing
        assert missing in result["error"]


def test_save_artifact_rejects_duplicate_execution_step_ids(artifact_home):
    # Evidence and the retrospective address a step by its ID, so a duplicate
    # would make the failing step ambiguous.
    artifact = json.loads(_minimal_valid_artifact("playbook.json"))
    step = {field: "value" for field in artifact_validation._EXECUTION_STEP_FIELDS}
    artifact["execution_steps"] = [dict(step), dict(step)]

    result = json.loads(_save("playbook.json", json.dumps(artifact)))

    assert result["ok"] is False
    assert "unique" in result["error"]


class TestEachRoleOnlySavesItsOwnArtifacts:
    """산출물의 작성 주체를 도구 경계로 강제한다.

    한 도구가 모든 파일명을 받으면 어느 역할이 무엇을 썼는지 서버가 알 수 없어, 분석
    역할이 리포트를 써도 완료 게이트를 통과한다. 그러면 역할 분리가 프롬프트 지시로만
    남는다.
    """

    @pytest.mark.parametrize("filename", ["report.md", "playbook.json"])
    def test_analysis_cannot_save_report_artifacts(self, artifact_home, filename):
        content = _minimal_valid_artifact(filename)

        result = json.loads(mcp_server.save_analysis_artifact(filename, content))

        assert result["ok"] is False
        assert not (artifact_home / filename).exists()

    @pytest.mark.parametrize("filename", ["scoping.json", "hypotheses.json", "validation-1.json"])
    def test_report_cannot_save_analysis_artifacts(self, artifact_home, filename):
        content = _minimal_valid_artifact(filename)

        result = json.loads(mcp_server.save_report_artifact(filename, content))

        assert result["ok"] is False
        assert not (artifact_home / filename).exists()

    def test_refusal_names_the_role_so_the_agent_stops_retrying(self, artifact_home):
        result = json.loads(mcp_server.save_analysis_artifact("report.md", _minimal_valid_artifact("report.md")))

        # 형태 오류처럼 읽히면 에이전트가 내용을 고쳐 같은 도구로 다시 시도한다.
        assert "role" in result["error"]
        assert "담당 역할" in result["error"]
