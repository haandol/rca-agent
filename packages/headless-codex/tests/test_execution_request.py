import json

import pytest

from headless_codex.services.execution_request import (
    InvalidExecutionRequestError,
    parse_execution_request,
)

APPROVAL = {
    "execution_id": "execution-1",
    "rca_id": "rca-1",
    "engine": "headless-codex",
    "approval_id": "approval-1",
    "requested_by": "operator@example.com",
    "report_s3_key": "reports/headless-codex/rca-1/report.md",
    "approved_playbook_s3_key": "approved/rca-1/execution-1/playbook.json",
    "playbook_digest": "a" * 64,
}


def test_a_user_approval_becomes_an_execution_request():
    request = parse_execution_request(json.dumps(APPROVAL))

    assert request.rca_id == "rca-1"
    assert request.engine == "headless-codex"
    assert request.requested_by == "operator@example.com"


def test_the_request_uses_the_precreated_execution_id():
    first = parse_execution_request(json.dumps(APPROVAL))

    assert first.execution_id == "execution-1"


@pytest.mark.parametrize("engine", ["strands", "headless-codex", "codex-headless", "cc-headless"])
def test_active_and_legacy_contract_engines_are_accepted(engine):
    request = parse_execution_request(json.dumps({**APPROVAL, "engine": engine}))

    assert request.engine == engine


@pytest.mark.parametrize("missing", APPROVAL)
def test_a_request_without_an_approval_subject_is_rejected(missing):
    payload = {key: value for key, value in APPROVAL.items() if key != missing}

    with pytest.raises(InvalidExecutionRequestError):
        parse_execution_request(json.dumps(payload))


def test_an_unknown_engine_is_rejected():
    with pytest.raises(InvalidExecutionRequestError):
        parse_execution_request(json.dumps({**APPROVAL, "engine": "other"}))


@pytest.mark.parametrize("digest", ["", "abc", "g" * 64])
def test_an_invalid_playbook_digest_is_rejected(digest):
    with pytest.raises(InvalidExecutionRequestError):
        parse_execution_request(json.dumps({**APPROVAL, "playbook_digest": digest}))


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_approval_identifier_is_rejected(blank):
    with pytest.raises(InvalidExecutionRequestError):
        parse_execution_request(json.dumps({**APPROVAL, "approval_id": blank}))


def test_an_alarm_notification_is_not_an_execution_request():
    """알람이 실행 요청으로 오인되면 승인 없이 실행이 시작된다."""
    alarm = {
        "AlarmName": "HighCPU",
        "NewStateValue": "ALARM",
        "Trigger": {"MetricName": "CPUUtilization"},
        "rca_id": "rca-1",
        "engine": "headless-codex",
        "approval_id": "not-an-approval",
    }

    with pytest.raises(InvalidExecutionRequestError):
        parse_execution_request(json.dumps(alarm))


@pytest.mark.parametrize("body", ["", "not json", "[]", "null", '"string"'])
def test_an_unreadable_message_is_rejected(body):
    with pytest.raises(InvalidExecutionRequestError):
        parse_execution_request(body)
