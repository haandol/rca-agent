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
    assert kwargs["MessageAttributes"]["event_type"]["StringValue"] == "rca_complete"
    body = json.loads(kwargs["Message"])
    assert body["root_cause"] == "database connection pool exhausted"
    assert body["alarm_context"]["metric_name"] == "DatabaseConnections"


@patch(
    "rca_agent.adapters.secondary.notification.sns_notification.SNS_NOTIFICATION_TOPIC_ARN",
    "arn:aws:sns:us-east-1:123:rca-topic",
)
def test_the_only_published_event_type_is_analysis_completion():
    """실행은 이 토픽을 거치지 않는다. 알림이 실행을 트리거하지 않기 때문이다."""
    msg = NotificationMessage(
        rca_id="rca-1",
        publication_id="rca-1",
        root_cause_summary="db pool exhausted",
        severity="medium",
    )
    mock_sns = MagicMock()

    SnsNotificationAdapter(sns_client=mock_sns).send(msg)

    kwargs = mock_sns.publish.call_args[1]
    assert kwargs["MessageAttributes"]["event_type"]["StringValue"] == "rca_complete"
    body = json.loads(kwargs["Message"])
    assert body["publication_id"] == "rca-1"
    assert "verification_status" not in body
