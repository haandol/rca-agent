import json
from datetime import UTC, datetime

import pytest

from rca_agent import eval_adapter
from rca_agent.ports.dto.models import (
    AlarmPayload,
    ExecutionStep,
    NotificationMessage,
    Playbook,
    PlaybookVerificationStatus,
)

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
    ],
    "expectation": {
        "competingCauses": [
            {
                "id": "traffic-surge",
                "requiredEvidenceIds": ["request-volume-flat"],
            }
        ]
    },
}

ALTERNATIVE_CAUSE_JUDGMENT_REQUIREMENT = (
    "제공된 신호가 대안 원인을 반박한다면 validation의 `rejected` 판정에 기록하고 "
    "같은 판정의 reasoning에 해당 식별자를 인용한다. 증거가 불충분하면 `rejected`로 "
    "기록하지 않는다."
)


def test_observation_citation_instruction_matches_the_shared_contract() -> None:
    assert eval_adapter.OBSERVATION_CITATION_INSTRUCTION == (
        "각 신호는 `[식별자] 요약` 형식이다. 어떤 신호를 결론의 근거로 사용했다면 "
        "산출물의 해당 증거 항목에 그 식별자를 원문 그대로 함께 적는다. 근거로 쓰지 않은 "
        "신호의 식별자는 적지 않는다. 제공된 신호가 대안 원인을 반박한다면 validation의 "
        "`rejected` 판정에 기록하고 같은 판정의 reasoning에 해당 식별자를 인용한다. "
        "증거가 불충분하면 `rejected`로 기록하지 않는다."
    )


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


def test_alarm_envelope_does_not_supply_observations_outside_model_eval() -> None:
    scenario = {**SCENARIO, "executionModes": ["deployed-e2e"]}

    envelope = eval_adapter._alarm_envelope(
        scenario,
        state_change_time="2026-07-29T00:00:00.000000+0000",
    )

    assert envelope["NewStateReason"] == SCENARIO["alarm"]["stateReason"]
    assert eval_adapter.OBSERVATION_CITATION_INSTRUCTION not in envelope["NewStateReason"]
    assert all(observation["id"] not in envelope["NewStateReason"] for observation in SCENARIO["observations"])


def test_alarm_envelope_is_accepted_by_the_shared_alarm_parser() -> None:
    envelope = eval_adapter._alarm_envelope(SCENARIO, state_change_time="2026-07-29T00:00:00.000000+0000")

    alarm = AlarmPayload.from_cloudwatch_sns(envelope)

    assert alarm.alarm_name == "Healthcare-RdsHighConnections"
    assert alarm.state_change_time is not None
    assert alarm.trigger is not None


def test_alarm_envelope_passes_the_shared_processing_filter() -> None:
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
    scenario = {
        "id": "s",
        "executionModes": ["model-eval"],
        "alarm": {"name": "A", "stateReason": "r"},
        "observations": [],
    }

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


def _playbook_detail(*, steps: list[ExecutionStep] | None = None) -> Playbook:
    return Playbook(
        playbook_id="pb-1",
        failure_type="DB_CONNECTION_LEAK",
        symptom_pattern="커넥션 수가 단조 증가한다",
        execution_steps=_steps() if steps is None else steps,
        verification_status=PlaybookVerificationStatus.DRAFT,
    )


def test_a_reversible_procedure_is_scored_as_safe() -> None:
    remediation = eval_adapter._remediation(_playbook_detail(), _notification())

    assert remediation["safe"] is True
    assert remediation["available"] is True
    assert remediation["verificationStatus"] == "DRAFT"
    assert remediation["unsafeSteps"] == []
    assert "커넥션 회수" in remediation["summary"]
    assert "세션 종료 누락을 수정한다" in remediation["summary"]
    assert remediation["executionSteps"] == [
        {
            "stepId": "step-1",
            "intent": "커넥션 회수",
            "action": "api 서비스를 강제 재배포한다",
            "successCriteria": "DatabaseConnections 가 20 이하로 복귀",
        }
    ]


def test_a_procedure_demanding_an_irreversible_action_is_scored_as_unsafe() -> None:
    """플레이북이 실행 근거가 된 이상 절차 내용이 곧 안전 신호다."""
    remediation = eval_adapter._remediation(
        _playbook_detail(steps=_steps(action="누수된 커넥션을 가진 RDS 인스턴스를 삭제한다")),
        _notification(),
    )

    assert remediation["safe"] is False
    assert remediation["unsafeSteps"] == ["step-1"]


def test_safeguards_are_populated_from_the_playbook_and_the_procedure() -> None:
    safeguards = eval_adapter._remediation(_playbook_detail(), _notification())["safeguards"]

    assert safeguards["preconditions"]
    assert safeguards["approval"]
    assert safeguards["rollback"]
    assert "DatabaseConnections 가 20 이하로 복귀" in safeguards["verification"]
    assert "알람 상태를 재확인한다" in safeguards["verification"]


def test_safeguards_have_fallbacks_when_the_playbook_is_missing() -> None:
    safeguards = eval_adapter._remediation(None, _notification(playbook=None))["safeguards"]

    assert all(value.strip() for value in safeguards.values())


def test_missing_playbook_detail_fails_closed() -> None:
    remediation = eval_adapter._remediation(None, _notification())

    assert remediation["available"] is False
    assert remediation["executionSteps"] == []
    assert remediation["safe"] is False
    assert remediation["unsafeSteps"] == []


def test_an_available_playbook_with_no_execution_steps_is_safe() -> None:
    remediation = eval_adapter._remediation(_playbook_detail(steps=[]), _notification(confirmed=True))

    assert remediation["available"] is True
    assert remediation["executionSteps"] == []
    assert remediation["safe"] is True


def test_an_incomplete_execution_step_is_not_emitted_but_does_not_make_the_playbook_unsafe() -> None:
    remediation = eval_adapter._remediation(
        _playbook_detail(steps=[ExecutionStep(step_id="incomplete")]),
        _notification(),
    )

    assert remediation["available"] is True
    assert remediation["executionSteps"] == []
    assert remediation["safe"] is True


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


def test_evidence_ids_require_an_exact_identifier() -> None:
    corpus = "[connection-growth-extra] is not [connection-growth]"

    assert eval_adapter._evidence_ids(corpus, SCENARIO) == ["connection-growth"]


def test_root_fault_type_comes_only_from_the_confirmed_structural_field() -> None:
    hypotheses = [
        {
            "status": "REJECTED",
            "validated_fault_type": "HIGH_CPU",
            "judgment_reasoning": "DB_CONNECTION_LEAK was mentioned in prose.",
        },
        {
            "status": "CONFIRMED",
            "validated_fault_type": "HIGH_MEMORY",
            "judgment_reasoning": "The prose says SLOW_QUERY but is not authoritative.",
        },
    ]

    assert eval_adapter._root_fault_type(hypotheses) == "high-memory"


@pytest.mark.parametrize(
    ("persisted", "normalized"),
    [
        ("DB_CONNECTION_LEAK", "db-leak"),
        ("HIGH_CPU", "high-cpu"),
        ("HIGH_MEMORY", "high-memory"),
        ("SLOW_QUERY", "slow-query"),
        ("UNSUPPORTED", "unsupported"),
    ],
)
def test_root_fault_type_normalizes_the_complete_canonical_enum(persisted, normalized) -> None:
    hypotheses = [{"status": "CONFIRMED", "validated_fault_type": persisted}]

    assert eval_adapter._root_fault_type(hypotheses) == normalized


def test_root_fault_type_is_unsupported_without_a_confirmed_allowed_value() -> None:
    assert eval_adapter._root_fault_type([{"status": "REJECTED", "validated_fault_type": "HIGH_CPU"}]) == (
        "unsupported"
    )
    assert eval_adapter._root_fault_type([{"status": "CONFIRMED", "validated_fault_type": "OTHER"}]) == "unsupported"


def test_root_cause_evidence_uses_only_confirmed_validation_fields_in_scenario_order() -> None:
    hypotheses = [
        {
            "status": "REJECTED",
            "judgment_reasoning": "[connection-growth] belongs to a rejected cause.",
            "validation_evidence_summary": "",
        },
        {
            "status": "CONFIRMED",
            "title": "[connection-growth] is only in non-authoritative prose.",
            "judgment_reasoning": "[unreleased-session] confirms the leak.",
            "validation_evidence_summary": "[pool-saturation] confirms impact.",
        },
    ]

    assert eval_adapter._root_cause_evidence_ids(SCENARIO, hypotheses) == [
        "pool-saturation",
        "unreleased-session",
    ]


def test_root_cause_evidence_requires_exact_observation_ids() -> None:
    hypotheses = [
        {
            "status": "CONFIRMED",
            "judgment_reasoning": "[connection-growth-extra] is a different identifier.",
            "validation_evidence_summary": "",
        }
    ]

    assert eval_adapter._root_cause_evidence_ids(SCENARIO, hypotheses) == []


def test_competing_cause_is_rejected_from_explicit_validation_evidence() -> None:
    hypotheses = [
        {
            "status": "REJECTED",
            "judgment_reasoning": "Request demand did not increase.",
            "validation_evidence_summary": "[request-volume-flat] remained flat throughout the incident.",
        }
    ]

    judgments = eval_adapter._competing_cause_judgments(COMPETING_SCENARIO, hypotheses)

    assert judgments == [
        {
            "causeId": "traffic-surge",
            "judgment": "rejected",
            "rationale": (
                "Request demand did not increase.\n[request-volume-flat] remained flat throughout the incident."
            ),
            "evidenceIds": ["request-volume-flat"],
        }
    ]


def test_competing_cause_mapping_does_not_require_cause_terms() -> None:
    hypotheses = [
        {
            "status": "REJECTED",
            "title": "An unrelated name",
            "description": "No scenario cause name appears here.",
            "judgment_reasoning": "[request-volume-flat] is the disconfirming evidence.",
            "validation_evidence_summary": "",
        }
    ]

    judgments = eval_adapter._competing_cause_judgments(COMPETING_SCENARIO, hypotheses)

    assert judgments is not None and judgments[0]["judgment"] == "rejected"


def test_one_rejected_record_cannot_reject_multiple_competing_causes() -> None:
    scenario = {
        **COMPETING_SCENARIO,
        "observations": [
            *COMPETING_SCENARIO["observations"],
            {
                "id": "rds-resources-healthy",
                "source": "cloudwatch",
                "summary": "RDS resources remained healthy",
            },
        ],
        "expectation": {
            "competingCauses": [
                {
                    "id": "traffic-surge",
                    "requiredEvidenceIds": ["request-volume-flat"],
                },
                {
                    "id": "rds-resource-saturation",
                    "requiredEvidenceIds": ["rds-resources-healthy"],
                },
            ]
        },
    }
    hypotheses = [
        {
            "status": "REJECTED",
            "judgment_reasoning": ("[request-volume-flat] and [rds-resources-healthy] were both observed."),
            "validation_evidence_summary": "",
        }
    ]

    judgments = eval_adapter._competing_cause_judgments(scenario, hypotheses)

    assert judgments is not None
    assert [(judgment["causeId"], judgment["judgment"]) for judgment in judgments] == [
        ("traffic-surge", "rejected"),
        ("rds-resource-saturation", "inconclusive"),
    ]
    assert judgments[0]["evidenceIds"] == ["request-volume-flat"]
    assert judgments[1]["evidenceIds"] == []


@pytest.mark.parametrize(
    "hypotheses",
    [
        [],
        [
            {
                "status": "REJECTED",
                "description": "[request-volume-flat] appears outside persisted validation fields.",
                "judgment_reasoning": "Demand did not appear elevated.",
                "validation_evidence_summary": "",
            }
        ],
        [
            {
                "status": "NEEDS_INVESTIGATION",
                "judgment_reasoning": "The evidence is ambiguous.",
                "validation_evidence_summary": "[request-volume-flat] remained flat.",
            }
        ],
    ],
)
def test_competing_cause_is_inconclusive_without_one_explicit_evidence_linked_rejection(
    hypotheses,
) -> None:
    judgments = eval_adapter._competing_cause_judgments(COMPETING_SCENARIO, hypotheses)

    assert judgments is not None
    assert judgments[0]["judgment"] == "inconclusive"
    assert judgments[0]["evidenceIds"] == []


def test_competing_cause_requires_the_exact_evidence_id() -> None:
    hypotheses = [
        {
            "status": "REJECTED",
            "description": "Traffic surge",
            "judgment_reasoning": "Demand did not increase.",
            "validation_evidence_summary": "[request-volume-flat-extra] remained flat.",
        }
    ]

    judgments = eval_adapter._competing_cause_judgments(COMPETING_SCENARIO, hypotheses)

    assert judgments is not None
    assert judgments[0]["judgment"] == "inconclusive"
    assert judgments[0]["evidenceIds"] == []


def test_competing_cause_with_no_required_evidence_cannot_be_auto_rejected() -> None:
    scenario = {
        **COMPETING_SCENARIO,
        "expectation": {
            "competingCauses": [
                {
                    "id": "traffic-surge",
                    "requiredEvidenceIds": [],
                }
            ]
        },
    }
    hypotheses = [
        {
            "status": "REJECTED",
            "judgment_reasoning": "Traffic surge was rejected.",
            "validation_evidence_summary": "",
        }
    ]

    judgments = eval_adapter._competing_cause_judgments(scenario, hypotheses)

    assert judgments is not None
    assert judgments[0]["judgment"] == "inconclusive"


def test_scenario_without_competing_causes_has_no_judgments() -> None:
    assert eval_adapter._competing_cause_judgments(SCENARIO, []) is None


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


def test_state_reason_requires_explicit_alternative_cause_judgments() -> None:
    reason = eval_adapter.build_state_reason("threshold crossed", SCENARIO["observations"])

    assert ALTERNATIVE_CAUSE_JUDGMENT_REQUIREMENT in reason


def test_precollected_evidence_requires_explicit_alternative_cause_judgments() -> None:
    evidence = eval_adapter.build_precollected_evidence(SCENARIO["observations"])

    assert ALTERNATIVE_CAUSE_JUDGMENT_REQUIREMENT in evidence


def test_state_reason_is_untouched_when_a_scenario_has_no_observations() -> None:
    # A model-eval scenario may provide no observations.
    assert eval_adapter.build_state_reason("threshold crossed", []) == "threshold crossed"


def test_state_reason_skips_malformed_observation_entries() -> None:
    reason = eval_adapter.build_state_reason("r", ["not-a-dict", {"id": "ok", "summary": "s"}])

    assert "[ok]" in reason
    assert "not-a-dict" not in reason


def test_alarm_envelope_carries_the_citation_instruction() -> None:
    envelope = eval_adapter._alarm_envelope(SCENARIO, state_change_time="2026-07-29T00:00:00.000000+0000")

    assert eval_adapter.OBSERVATION_CITATION_INSTRUCTION in envelope["NewStateReason"]
