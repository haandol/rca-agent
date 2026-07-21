import json
from unittest.mock import MagicMock, patch

from rca_agent.adapters.secondary.notification.sns_notification import SnsNotificationAdapter
from rca_agent.ports.dto.models import AlarmContext, NotificationMessage


@patch(
    "rca_agent.adapters.secondary.notification.sns_notification.SNS_NOTIFICATION_TOPIC_ARN",
    "arn:aws:sns:us-east-1:123:rca-topic",
)
def test_publishes_event_type_attribute_and_alarm_context():
    msg = NotificationMessage(
        rca_id="rca-1",
        root_cause_summary="db pool exhausted",
        severity="high",
        root_cause="database connection pool exhausted",
        alarm_context=AlarmContext(
            alarm_name="Healthcare-RdsHighConnections",
            namespace="AWS/RDS",
            metric_name="DatabaseConnections",
            threshold=30.0,
        ),
    )
    mock_sns = MagicMock()

    adapter = SnsNotificationAdapter(sns_client=mock_sns)
    assert adapter.send(msg) is True

    kwargs = mock_sns.publish.call_args[1]
    # 구독 필터가 걸 수 있도록 event_type 이 메시지 속성으로 실린다
    assert kwargs["MessageAttributes"]["event_type"]["StringValue"] == "rca_complete"
    body = json.loads(kwargs["Message"])
    assert body["root_cause"] == "database connection pool exhausted"
    assert body["alarm_context"]["metric_name"] == "DatabaseConnections"


@patch(
    "rca_agent.adapters.secondary.notification.sns_notification.SNS_NOTIFICATION_TOPIC_ARN",
    "arn:aws:sns:us-east-1:123:rca-topic",
)
def test_remediation_result_uses_distinct_event_type():
    msg = NotificationMessage(
        rca_id="rca-1",
        root_cause_summary="Remediation succeeded",
        severity="medium",
        event_type="remediation_complete",
    )
    mock_sns = MagicMock()

    SnsNotificationAdapter(sns_client=mock_sns).send(msg)

    kwargs = mock_sns.publish.call_args[1]
    assert kwargs["MessageAttributes"]["event_type"]["StringValue"] == "remediation_complete"
