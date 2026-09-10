from threading import Event
from unittest.mock import MagicMock, patch

import pytest

from rca_agent import main as agent_main
from rca_agent.adapters.secondary.queue.sqs_consumer import SqsConsumer
from rca_agent.ports.interfaces.queue_consumer import QueueReceiveError


class _SingleIterationEvent:
    def __init__(self):
        self._checks = 0

    def is_set(self):
        self._checks += 1
        return self._checks > 1

    def set(self):
        self._checks = 2


@pytest.mark.parametrize(("processed", "acked"), [(True, True), (False, False)])
def test_primary_consumer_only_acks_successful_processing(processed, acked):
    consumer = MagicMock()
    consumer.poll.return_value = [({"AlarmName": "HighCPU"}, "receipt-1", 2, "message-1")]
    container = MagicMock(queue_consumer=consumer)
    orchestrator = MagicMock()
    orchestrator.process_alarm.return_value = processed

    with (
        patch.dict(
            agent_main.os.environ,
            {
                "SQS_QUEUE_URL": "https://sqs.example.test/rca",
                "SQS_POLL_WAIT_SECONDS": "0",
            },
        ),
        patch("rca_agent.adapters.primary.health.health_server.start_health_server"),
        patch("rca_agent.di.app_container.AppContainer", return_value=container),
        patch("rca_agent.services.pipeline.PipelineOrchestrator", return_value=orchestrator),
        patch.object(agent_main, "Event", return_value=_SingleIterationEvent()),
        patch.object(agent_main.signal, "signal"),
    ):
        agent_main.main()

    orchestrator.process_alarm.assert_called_once_with(
        {"AlarmName": "HighCPU"},
        receive_count=2,
        message_id="message-1",
    )
    assert consumer.ack.called is acked


def test_sqs_consumer_requests_and_yields_receive_count():
    sqs = MagicMock()
    sqs.receive_message.return_value = {
        "Messages": [
            {
                "Body": '{"AlarmName":"HighCPU"}',
                "ReceiptHandle": "receipt-1",
                "MessageId": "message-1",
                "Attributes": {"ApproximateReceiveCount": "3"},
            }
        ]
    }

    with patch("rca_agent.adapters.secondary.queue.sqs_consumer.boto3.client", return_value=sqs):
        consumer = SqsConsumer("https://sqs.example.test/rca", poll_wait_seconds=0)
        messages = list(consumer.poll())

    assert messages == [({"AlarmName": "HighCPU"}, "receipt-1", 3, "message-1")]
    assert sqs.receive_message.call_args.kwargs["AttributeNames"] == ["ApproximateReceiveCount"]


def test_sqs_consumer_does_not_yield_message_without_message_id():
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

    with patch("rca_agent.adapters.secondary.queue.sqs_consumer.boto3.client", return_value=sqs):
        consumer = SqsConsumer("https://sqs.example.test/rca", poll_wait_seconds=0)

        assert list(consumer.poll()) == []


def test_receive_failure_is_distinct_from_an_empty_queue():
    sqs = MagicMock()
    failure = RuntimeError("receive unavailable")
    sqs.receive_message.side_effect = [failure, {}]
    with patch("rca_agent.adapters.secondary.queue.sqs_consumer.boto3.client", return_value=sqs):
        consumer = SqsConsumer("https://sqs.example.test/rca")
        with pytest.raises(QueueReceiveError) as caught:
            list(consumer.poll())
        assert caught.value.__cause__ is failure
        assert list(consumer.poll()) == []


def _run_polling_loop(consumer, shutdown_event):
    orchestrator = MagicMock()
    with (
        patch.dict(agent_main.os.environ, {"SQS_QUEUE_URL": "offline"}),
        patch("rca_agent.adapters.primary.health.health_server.start_health_server"),
        patch("rca_agent.di.app_container.AppContainer", return_value=MagicMock(queue_consumer=consumer)),
        patch("rca_agent.services.pipeline.PipelineOrchestrator", return_value=orchestrator),
        patch.object(agent_main, "Event", return_value=shutdown_event),
        patch.object(agent_main.signal, "signal"),
    ):
        agent_main.main()
    return orchestrator


def test_receive_retry_wait_grows_is_bounded_and_resets_after_success():
    consumer = MagicMock()
    consumer.poll.side_effect = [QueueReceiveError("unavailable")] * 7 + [
        [],
        QueueReceiveError("unavailable again"),
    ]
    shutdown_event = MagicMock()
    shutdown_event.is_set.side_effect = [False] * 9 + [True]

    orchestrator = _run_polling_loop(consumer, shutdown_event)

    assert [call.args[0] for call in shutdown_event.wait.call_args_list] == [1, 2, 4, 8, 16, 30, 30, 1]
    assert consumer.poll.call_count == 9
    orchestrator.process_alarm.assert_not_called()
    consumer.ack.assert_not_called()


def test_shutdown_during_receive_retry_wait_prevents_another_poll():
    consumer = MagicMock()
    consumer.poll.side_effect = QueueReceiveError("unavailable")
    shutdown_event = Event()
    with patch.object(shutdown_event, "wait", side_effect=lambda _: shutdown_event.set()) as wait:
        _run_polling_loop(consumer, shutdown_event)
    wait.assert_called_once_with(1)
    consumer.poll.assert_called_once()
