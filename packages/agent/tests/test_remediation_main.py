from unittest.mock import MagicMock, patch

from rca_agent import remediation_main


class _SingleIterationEvent:
    def __init__(self):
        self._checks = 0

    def is_set(self):
        self._checks += 1
        return self._checks > 1

    def set(self):
        self._checks = 2


def test_failed_consumer_processing_is_not_acked():
    consumer = MagicMock()
    consumer.poll.return_value = [({"rca_id": "rca-1", "confirmed": True}, "receipt-1")]
    container = MagicMock(queue_consumer=consumer)
    orchestrator = MagicMock()
    orchestrator.process_notification.side_effect = RuntimeError("processing failed")

    with (
        patch.dict(
            remediation_main.os.environ,
            {
                "REMEDIATION_QUEUE_URL": "https://sqs.example.test/remediation",
                "REMEDIATION_SQS_POLL_WAIT_SECONDS": "0",
            },
        ),
        patch(
            "rca_agent.adapters.primary.health.health_server.start_health_server",
        ),
        patch(
            "rca_agent.di.app_container.AppContainer",
            return_value=container,
        ),
        patch(
            "rca_agent.services.remediation_pipeline.RemediationOrchestrator",
            return_value=orchestrator,
        ),
        patch.object(remediation_main, "Event", return_value=_SingleIterationEvent()),
        patch.object(remediation_main.signal, "signal"),
    ):
        remediation_main.main()

    orchestrator.process_notification.assert_called_once()
    consumer.ack.assert_not_called()
