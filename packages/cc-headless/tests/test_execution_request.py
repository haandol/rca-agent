import json

import pytest

from cc_headless.services.execution_request import (
    InvalidExecutionRequestError,
    parse_execution_request,
)

APPROVAL = {
    "rca_id": "rca-1",
    "engine": "cc-headless",
    "approval_id": "approval-1",
    "requested_by": "operator@example.com",
}


def test_a_user_approval_becomes_an_execution_request():
    request = parse_execution_request(json.dumps(APPROVAL))

    assert request.rca_id == "rca-1"
    assert request.engine == "cc-headless"
    assert request.requested_by == "operator@example.com"


def test_the_same_approval_always_derives_the_same_execution_id():
    """재전달이 중복 실행이 되지 않으려면 식별자가 요청에서 결정론적으로 나와야 한다."""
    first = parse_execution_request(json.dumps(APPROVAL))
    second = parse_execution_request(json.dumps({**APPROVAL, "requested_by": "someone-else"}))

    assert first.execution_id == second.execution_id


def test_a_different_approval_derives_a_different_execution_id():
    first = parse_execution_request(json.dumps(APPROVAL))
    second = parse_execution_request(json.dumps({**APPROVAL, "approval_id": "approval-2"}))
    other_engine = parse_execution_request(json.dumps({**APPROVAL, "engine": "strands"}))

    assert len({first.execution_id, second.execution_id, other_engine.execution_id}) == 3


@pytest.mark.parametrize("missing", ["rca_id", "engine", "approval_id"])
def test_a_request_without_an_approval_subject_is_rejected(missing):
    payload = {key: value for key, value in APPROVAL.items() if key != missing}

    with pytest.raises(InvalidExecutionRequestError):
        parse_execution_request(json.dumps(payload))


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
        "engine": "cc-headless",
        "approval_id": "not-an-approval",
    }

    with pytest.raises(InvalidExecutionRequestError):
        parse_execution_request(json.dumps(alarm))


@pytest.mark.parametrize("body", ["", "not json", "[]", "null", '"string"'])
def test_an_unreadable_message_is_rejected(body):
    with pytest.raises(InvalidExecutionRequestError):
        parse_execution_request(body)
