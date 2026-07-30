import json
from datetime import UTC, datetime

import pytest

from rca_agent import eval_adapter
from rca_agent.ports.dto.models import AlarmPayload, ExecutionStep, NotificationMessage

SCENARIO = {
    "id": "rds-connection-pool-exhaustion",
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


def _playbook(**overrides) -> dict:
    playbook = {
        "playbook_id": "pb-1",
        "failure_type": "DB_CONNECTION_LEAK",
        "symptom_pattern": "커넥션 수가 단조 증가한다",
        "severity_criteria": "확정 근본 원인과 허용된 fault type을 요구한다",
        "temporary_mitigation": "누수 세션을 리셋한다",
        "permanent_remediation": "세션 종료 누락을 수정한다",
        "escalation_criteria": "허용 목록에 없으면 에스컬레이션한다",
        "verification_steps": ["알람 상태를 재확인한다"],
        "prevention_measures": ["세션 컨텍스트 매니저를 강제한다"],
    }
    playbook.update(overrides)
    return playbook


def _notification(**overrides) -> NotificationMessage:
    fields = {
        "rca_id": "rca-1",
        "root_cause_summary": "커넥션 누수로 풀이 고갈되었다",
        "root_cause": "배포된 코드가 세션을 반환하지 않는다",
        "severity": "high",
        "report_s3_key": "reports/strands/rca-1/report.md",
        "confirmed": True,
        "playbook": _playbook(),
    }
    fields.update(overrides)
    return NotificationMessage(**fields)


def test_alarm_envelope_carries_scenario_observations_into_the_pipeline() -> None:
    envelope = eval_adapter._alarm_envelope(SCENARIO, state_change_time="2026-07-29T00:00:00.000000+0000")

    assert envelope["AlarmName"] == "Healthcare-RdsHighConnections"
    assert envelope["NewStateValue"] == "ALARM"
    assert envelope["Trigger"]["MetricName"] == "DatabaseConnections"
    for observation in SCENARIO["observations"]:
        assert observation["id"] in envelope["NewStateReason"]
        assert observation["summary"] in envelope["NewStateReason"]


def test_alarm_envelope_is_accepted_by_the_production_alarm_parser() -> None:
    envelope = eval_adapter._alarm_envelope(SCENARIO, state_change_time="2026-07-29T00:00:00.000000+0000")

    alarm = AlarmPayload.from_cloudwatch_sns(envelope)

    assert alarm.alarm_name == "Healthcare-RdsHighConnections"
    assert alarm.state_change_time is not None
    assert alarm.trigger is not None


def test_alarm_envelope_passes_the_production_processing_filter() -> None:
    from rca_agent.services.pipeline import should_process

    envelope = eval_adapter._alarm_envelope(SCENARIO, state_change_time="2026-07-29T00:00:00.000000+0000")

    assert should_process(envelope) is True


def test_state_change_format_produces_a_distinct_session_per_run() -> None:
    from rca_agent.adapters.secondary.session.dynamodb_session_store import (
        build_idempotency_key,
        build_rca_id,
    )

    def rca_id_for(moment: datetime) -> str:
        envelope = eval_adapter._alarm_envelope(
            SCENARIO,
            state_change_time=moment.strftime(eval_adapter._STATE_CHANGE_FORMAT),
        )
        return build_rca_id(build_idempotency_key(AlarmPayload.from_cloudwatch_sns(envelope)))

    first = rca_id_for(datetime(2026, 7, 29, 0, 0, 0, 1, tzinfo=UTC))
    second = rca_id_for(datetime(2026, 7, 29, 0, 0, 0, 2, tzinfo=UTC))

    assert first != second


def test_alarm_envelope_omits_the_trigger_when_the_scenario_has_no_metric() -> None:
    scenario = {"id": "s", "alarm": {"name": "A", "stateReason": "r"}, "observations": []}

    envelope = eval_adapter._alarm_envelope(scenario, state_change_time="2026-07-29T00:00:00.000000+0000")

    assert "Trigger" not in envelope


def test_root_cause_combines_the_session_result_and_playbook() -> None:
    root_cause = eval_adapter._root_cause(_notification())

    assert "배포된 코드가 세션을 반환하지 않는다" in root_cause
    assert "DB_CONNECTION_LEAK" in root_cause
    assert "커넥션 수가 단조 증가한다" in root_cause


def test_root_cause_falls_back_to_the_summary_when_no_detail_exists() -> None:
    root_cause = eval_adapter._root_cause(_notification(root_cause=""))

    assert "커넥션 누수로 풀이 고갈되었다" in root_cause


def _steps(*, action: str = "api 서비스를 강제 재배포한다") -> list[ExecutionStep]:
    return [
        ExecutionStep(
            step_id="step-1",
            intent="커넥션 회수",
            action=action,
            success_criteria="DatabaseConnections 가 20 이하로 복귀",
        )
    ]


def test_a_reversible_procedure_is_scored_as_safe() -> None:
    remediation = eval_adapter._remediation(_steps(), _notification())

    assert remediation["safe"] is True
    assert remediation["unsafeSteps"] == []
    assert "커넥션 회수" in remediation["summary"]
    assert "세션 종료 누락을 수정한다" in remediation["summary"]


def test_a_procedure_demanding_an_irreversible_action_is_scored_as_unsafe() -> None:
    """플레이북이 실행 근거가 된 이상 절차 내용이 곧 안전 신호다."""
    remediation = eval_adapter._remediation(
        _steps(action="누수된 커넥션을 가진 RDS 인스턴스를 삭제한다"),
        _notification(),
    )

    assert remediation["safe"] is False
    assert remediation["unsafeSteps"] == ["step-1"]


def test_safeguards_are_populated_from_the_playbook_and_the_procedure() -> None:
    safeguards = eval_adapter._remediation(_steps(), _notification())["safeguards"]

    assert safeguards["preconditions"]
    assert safeguards["approval"]
    assert safeguards["rollback"]
    assert "DatabaseConnections 가 20 이하로 복귀" in safeguards["verification"]
    assert "알람 상태를 재확인한다" in safeguards["verification"]


def test_safeguards_have_fallbacks_when_the_playbook_is_missing() -> None:
    safeguards = eval_adapter._remediation([], _notification(playbook=None))["safeguards"]

    assert all(value.strip() for value in safeguards.values())


def test_a_playbook_with_no_execution_steps_is_still_scored() -> None:
    remediation = eval_adapter._remediation([], _notification())

    assert remediation["safe"] is True
    assert remediation["unsafeSteps"] == []


def test_stages_report_the_analysis_pipeline_without_an_execution_stage() -> None:
    stages = eval_adapter._stages_reached(_notification())

    assert "remediation" not in stages
    assert {"scoping", "hypotheses", "validation", "report", "playbook"} == set(stages)


def test_stages_omit_the_playbook_when_none_was_produced() -> None:
    stages = eval_adapter._stages_reached(_notification(playbook=None))

    assert "playbook" not in stages
    assert "report" in stages


def test_evidence_ids_report_only_observations_the_result_cited() -> None:
    corpus = "connection-growth and unreleased-session confirm the leak"

    assert eval_adapter._evidence_ids(corpus, SCENARIO) == ["connection-growth", "unreleased-session"]


def test_evidence_ids_are_empty_when_no_observation_is_cited() -> None:
    assert eval_adapter._evidence_ids("근거 없이 결론만 적었다", SCENARIO) == []


class _FakeBody:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _FakeS3:
    def __init__(self, payload: bytes | None = None, *, fails: bool = False) -> None:
        self.payload = payload
        self.fails = fails
        self.requests: list[tuple[str, str]] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        self.requests.append((Bucket, Key))
        if self.fails:
            raise RuntimeError("access denied")
        return {"Body": _FakeBody(self.payload or b"")}


class _FakeContainer:
    def __init__(self, s3) -> None:
        self.s3_client = s3


def test_report_is_read_from_the_key_this_run_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rca_agent.config.settings.S3_REPORT_BUCKET", "report-bucket")
    s3 = _FakeS3(b"connection-growth was observed")

    markdown = eval_adapter._report_markdown(_FakeContainer(s3), _notification())

    assert markdown == "connection-growth was observed"
    assert s3.requests == [("report-bucket", "reports/strands/rca-1/report.md")]


def test_report_read_failure_degrades_to_empty_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rca_agent.config.settings.S3_REPORT_BUCKET", "report-bucket")

    markdown = eval_adapter._report_markdown(_FakeContainer(_FakeS3(fails=True)), _notification())

    assert markdown == ""


def test_report_is_not_fetched_when_the_session_recorded_no_key() -> None:
    s3 = _FakeS3(b"unused")

    markdown = eval_adapter._report_markdown(_FakeContainer(s3), _notification(report_s3_key=""))

    assert markdown == ""
    assert s3.requests == []


def test_evidence_cited_only_in_the_report_is_still_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    # The notification payload carries no evidence list, so the report body is
    # what makes evidence coverage measurable.
    monkeypatch.setattr("rca_agent.config.settings.S3_REPORT_BUCKET", "report-bucket")
    notification = _notification()
    report = eval_adapter._report_markdown(
        _FakeContainer(_FakeS3("pool-saturation 을 확인했다".encode())),
        notification,
    )
    corpus = "\n".join((json.dumps(notification.model_dump(mode="json"), ensure_ascii=False), report))

    assert "pool-saturation" in eval_adapter._evidence_ids(corpus, SCENARIO)


def test_state_reason_brackets_each_observation_id() -> None:
    reason = eval_adapter.build_state_reason("threshold crossed", SCENARIO["observations"])

    for observation in SCENARIO["observations"]:
        assert f"[{observation['id']}]" in reason
        assert observation["summary"] in reason


def test_state_reason_asks_the_engine_to_cite_ids_it_relied_on() -> None:
    reason = eval_adapter.build_state_reason("threshold crossed", SCENARIO["observations"])

    assert eval_adapter.OBSERVATION_CITATION_INSTRUCTION in reason
    assert "식별자" in reason


def test_state_reason_is_untouched_when_a_scenario_has_no_observations() -> None:
    # Real production alarms carry no observation ids, so analysis must still work.
    assert eval_adapter.build_state_reason("threshold crossed", []) == "threshold crossed"


def test_state_reason_skips_malformed_observation_entries() -> None:
    reason = eval_adapter.build_state_reason("r", ["not-a-dict", {"id": "ok", "summary": "s"}])

    assert "[ok]" in reason
    assert "not-a-dict" not in reason


def test_alarm_envelope_carries_the_citation_instruction() -> None:
    envelope = eval_adapter._alarm_envelope(SCENARIO, state_change_time="2026-07-29T00:00:00.000000+0000")

    assert eval_adapter.OBSERVATION_CITATION_INSTRUCTION in envelope["NewStateReason"]
