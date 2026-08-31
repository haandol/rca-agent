from unittest.mock import MagicMock, patch

from headless_codex import main as headless_main


class _SingleIterationEvent:
    def __init__(self):
        self._checks = 0

    def is_set(self):
        self._checks += 1
        return self._checks > 1

    def set(self):
        self._checks = 2


def test_alarm_consumer_passes_sqs_identity_and_acknowledges_success():
    sqs = MagicMock()
    sqs.receive_message.return_value = {
        "Messages": [
            {
                "Body": '{"AlarmName":"HighCPU"}',
                "ReceiptHandle": "receipt-1",
                "MessageId": "message-1",
                "Attributes": {"ApproximateReceiveCount": "2"},
            }
        ]
    }
    orchestrator = MagicMock()
    orchestrator.process_message.return_value = True

    with (
        patch.object(headless_main, "SQS_QUEUE_URL", "https://sqs.example.test/alarm"),
        patch.object(headless_main, "SQS_POLL_WAIT_SECONDS", 0),
        patch.object(headless_main, "start_health_server"),
        patch.object(headless_main, "AppContainer", return_value=MagicMock()),
        patch.object(headless_main, "PipelineOrchestrator", return_value=orchestrator),
        patch.object(headless_main, "Event", return_value=_SingleIterationEvent()),
        patch.object(headless_main.signal, "signal"),
        patch.object(headless_main.boto3, "client", return_value=sqs),
    ):
        headless_main.main()

    orchestrator.process_message.assert_called_once_with(
        '{"AlarmName":"HighCPU"}',
        receive_count=2,
        message_id="message-1",
    )
    sqs.delete_message.assert_called_once_with(
        QueueUrl="https://sqs.example.test/alarm",
        ReceiptHandle="receipt-1",
    )


def test_alarm_consumer_leaves_message_without_identity_unacknowledged():
    sqs = MagicMock()
    sqs.receive_message.return_value = {
        "Messages": [
            {
                "Body": '{"AlarmName":"HighCPU"}',
                "ReceiptHandle": "receipt-1",
                "Attributes": {"ApproximateReceiveCount": "1"},
            }
        ]
    }
    orchestrator = MagicMock()

    with (
        patch.object(headless_main, "SQS_QUEUE_URL", "https://sqs.example.test/alarm"),
        patch.object(headless_main, "SQS_POLL_WAIT_SECONDS", 0),
        patch.object(headless_main, "start_health_server"),
        patch.object(headless_main, "AppContainer", return_value=MagicMock()),
        patch.object(headless_main, "PipelineOrchestrator", return_value=orchestrator),
        patch.object(headless_main, "Event", return_value=_SingleIterationEvent()),
        patch.object(headless_main.signal, "signal"),
        patch.object(headless_main.boto3, "client", return_value=sqs),
    ):
        headless_main.main()

    orchestrator.process_message.assert_not_called()
    sqs.delete_message.assert_not_called()
