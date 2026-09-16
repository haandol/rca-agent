import time
from threading import Event
from unittest.mock import MagicMock, patch

import pytest

from rca_agent import main as agent_main
from rca_agent.adapters.secondary.queue.sqs_consumer import SqsConsumer
from rca_agent.ports.interfaces.queue_consumer import QueueMessage, QueueReceiveError


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
    shutdown = Event()

    def poll():
        if not getattr(poll, "seen", False):
            poll.seen = True
            return [QueueMessage({"AlarmName": "HighCPU"}, "receipt-1", 2, "message-1", time.monotonic())]
        shutdown.set()
        return []

    consumer.poll.side_effect = poll
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
        patch.object(agent_main, "Event", return_value=shutdown),
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

    assert messages[0][:4] == ({"AlarmName": "HighCPU"}, "receipt-1", 3, "message-1")
    assert messages[0].received_at_monotonic <= time.monotonic()
    assert sqs.receive_message.call_args.kwargs["VisibilityTimeout"] == 10800
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


@pytest.mark.parametrize("response", [{}, {"ResponseMetadata": {"HTTPStatusCode": 500}}, RuntimeError("network")])
def test_sqs_renewal_requires_confirmed_success(response):
    from rca_agent.ports.interfaces.queue_consumer import QueueLeaseLostError

    sqs = MagicMock()
    if isinstance(response, Exception):
        sqs.change_message_visibility.side_effect = response
    else:
        sqs.change_message_visibility.return_value = response
    with patch("rca_agent.adapters.secondary.queue.sqs_consumer.boto3.client", return_value=sqs):
        consumer = SqsConsumer("queue")
    with pytest.raises(QueueLeaseLostError):
        consumer.renew_visibility("receipt", 10800)
    sqs.delete_message.assert_not_called()


def test_sqs_renewal_uses_current_receipt_and_bounded_config():
    sqs = MagicMock()
    sqs.change_message_visibility.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}
    with patch("rca_agent.adapters.secondary.queue.sqs_consumer.boto3.client", return_value=sqs) as create:
        consumer = SqsConsumer("queue")
    config = create.call_args.kwargs["config"]
    assert config.read_timeout <= 60 and config.retries["total_max_attempts"] == 1
    consumer.renew_visibility("current", 10800)
    sqs.change_message_visibility.assert_called_once_with(
        QueueUrl="queue", ReceiptHandle="current", VisibilityTimeout=10800
    )


def test_one_message_renewal_failure_does_not_stop_next_message():
    from rca_agent.utils.message_lease import bind_message_ownership

    consumer, shutdown, orchestrator = MagicMock(), Event(), MagicMock()
    sequence = iter(["first", "second"])

    def poll():
        key = next(sequence, None)
        if key is None:
            shutdown.set()
            return []
        return [QueueMessage({"id": key}, key, 1, key, time.monotonic())]

    consumer.poll.side_effect = poll
    consumer.renew_visibility.side_effect = [RuntimeError("first renewal failed"), None]

    def process(*args, **kwargs):
        bind_message_ownership(lambda: True)
        return True

    orchestrator.process_alarm.side_effect = process
    with (
        patch.dict(agent_main.os.environ, {"SQS_QUEUE_URL": "offline"}),
        patch("rca_agent.adapters.primary.health.health_server.start_health_server"),
        patch("rca_agent.di.app_container.AppContainer", return_value=MagicMock(queue_consumer=consumer)),
        patch("rca_agent.services.pipeline.PipelineOrchestrator", return_value=orchestrator),
        patch.object(agent_main, "Event", return_value=shutdown),
        patch.object(agent_main.signal, "signal"),
    ):
        agent_main.main()
    assert orchestrator.process_alarm.call_count == 2
    consumer.ack.assert_called_once_with("second")
    assert consumer.renew_visibility.call_count == 2
