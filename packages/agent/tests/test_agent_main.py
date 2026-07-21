from unittest.mock import MagicMock, patch

import pytest

from rca_agent import main as agent_main


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
    consumer.poll.return_value = [({"AlarmName": "HighCPU"}, "receipt-1")]
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

    orchestrator.process_alarm.assert_called_once()
    assert consumer.ack.called is acked
