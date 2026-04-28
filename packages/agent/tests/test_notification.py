import json
from unittest.mock import MagicMock, patch

from rca_agent.models import NotificationMessage, RcaReport
from rca_agent.services.notification import build_notification, send_notification


def _make_report(confirmed=True) -> RcaReport:
    return RcaReport(
        rca_id="rca-1",
        incident_summary="CPU spike",
        root_cause="Memory leak",
        root_cause_confirmed=confirmed,
        confidence_score=0.9,
    )


class TestBuildNotification:
    def test_confirmed(self):
        report = _make_report(confirmed=True)
        msg = build_notification(report, "reports/rca-1.md", 600)

        assert msg.rca_id == "rca-1"
        assert msg.confirmed
        assert msg.severity == "high"
        assert msg.elapsed_seconds == 600

    def test_unconfirmed(self):
        report = _make_report(confirmed=False)
        msg = build_notification(report, "reports/rca-1.md", 1200)

        assert not msg.confirmed
        assert "manual review" in msg.root_cause_summary.lower()
        assert msg.severity == "medium"


class TestSendNotification:
    def test_skips_when_not_configured(self):
        msg = NotificationMessage(rca_id="r-1", root_cause_summary="t", severity="high")
        assert not send_notification(msg)

    @patch("rca_agent.services.notification.SNS_NOTIFICATION_TOPIC_ARN", "arn:aws:sns:us-east-1:123:rca-topic")
    def test_publishes_to_sns(self):
        msg = NotificationMessage(rca_id="r-1", root_cause_summary="Memory leak", severity="high")
        mock_sns = MagicMock()

        result = send_notification(msg, sns_client=mock_sns)

        assert result is True
        mock_sns.publish.assert_called_once()
        call_kwargs = mock_sns.publish.call_args[1]
        assert "rca-topic" in call_kwargs["TopicArn"]
        body = json.loads(call_kwargs["Message"])
        assert body["rca_id"] == "r-1"

    @patch("rca_agent.services.notification.SNS_NOTIFICATION_TOPIC_ARN", "arn:aws:sns:us-east-1:123:rca-topic")
    def test_retries_on_failure(self):
        msg = NotificationMessage(rca_id="r-1", root_cause_summary="t", severity="high")
        mock_sns = MagicMock()
        mock_sns.publish.side_effect = [RuntimeError("transient"), None]

        result = send_notification(msg, sns_client=mock_sns, base_delay=0.01)

        assert result is True
        assert mock_sns.publish.call_count == 2

    @patch("rca_agent.services.notification.SNS_NOTIFICATION_TOPIC_ARN", "arn:aws:sns:us-east-1:123:rca-topic")
    def test_exhausts_retries(self):
        msg = NotificationMessage(rca_id="r-1", root_cause_summary="t", severity="high")
        mock_sns = MagicMock()
        mock_sns.publish.side_effect = RuntimeError("persistent")

        result = send_notification(msg, sns_client=mock_sns, max_retries=2, base_delay=0.01)

        assert result is False
        assert mock_sns.publish.call_count == 2

    @patch("rca_agent.services.notification.SNS_NOTIFICATION_TOPIC_ARN", "arn:aws:sns:us-east-1:123:rca-topic")
    @patch("rca_agent.services.notification.S3_REPORT_BUCKET", "my-bucket")
    def test_includes_presigned_url(self):
        msg = NotificationMessage(rca_id="r-1", root_cause_summary="t", severity="high", report_s3_key="reports/r-1.md")
        mock_sns = MagicMock()
        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/presigned"

        send_notification(msg, sns_client=mock_sns, s3_client=mock_s3)

        body = json.loads(mock_sns.publish.call_args[1]["Message"])
        assert body["report_url"] == "https://s3.example.com/presigned"
