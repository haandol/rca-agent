"""Pending publication progresses between approval batches without repeating approved work."""

from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from headless_codex import execution_main


@pytest.mark.parametrize("messages", [[], [{"Body": "approved-body", "ReceiptHandle": "receipt"}]])
def test_each_poll_turn_services_followups_even_with_approval_messages(monkeypatch, messages):
    stop = Event()
    order = []
    process = Mock(side_effect=lambda body: order.append("approved") or True)

    def tick():
        """End after one loop turn; a permanently nonempty test queue cannot starve this callback."""
        order.append("followup")
        stop.set()

    sqs = Mock()
    sqs.receive_message.return_value = {"Messages": messages}
    monkeypatch.setattr(execution_main, "EXECUTION_QUEUE_URL", "local-queue")
    monkeypatch.setattr(execution_main, "start_health_server", Mock())
    monkeypatch.setattr(execution_main, "setup_logging", Mock())
    monkeypatch.setattr(execution_main, "Event", lambda: stop)
    monkeypatch.setattr(execution_main.signal, "signal", Mock())
    monkeypatch.setattr(execution_main.boto3, "client", lambda service: sqs)
    monkeypatch.setattr(
        execution_main,
        "AppExecutionContainer",
        lambda: SimpleNamespace(deferred_publication=SimpleNamespace(tick=tick)),
    )
    monkeypatch.setattr(
        execution_main,
        "ExecutionOrchestrator",
        lambda *a, **kw: SimpleNamespace(process_message=process),
    )
    execution_main.main()
    assert order == (["approved", "followup"] if messages else ["followup"])
    assert process.call_count == len(messages)
    assert sqs.delete_message.call_count == len(messages)
    sqs.receive_message.assert_called_once()
