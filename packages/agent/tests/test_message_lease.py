"""Receipt renewal is independent of model runtime, but uncertain ownership never authorizes ACK."""

import threading
from unittest.mock import MagicMock

import pytest

from rca_agent.ports.interfaces.queue_consumer import QueueLeaseLostError, QueueMessage
from rca_agent.utils.message_lease import (
    MessageLease,
    check_message_lease,
    get_message_cancellation_check,
    message_cancelled,
    message_processing_scope,
)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def setup():
    clock, consumer, shutdown = Clock(), MagicMock(), threading.Event()
    message = QueueMessage({"AlarmName": "observed"}, "receipt-current", 2, "message", clock())
    lease = MessageLease(consumer, message, shutdown, clock=clock, heartbeat_seconds=3600, request_bound=65)
    lease.bind_ownership(lambda: True)
    return lease, consumer, clock, shutdown


def test_initial_visibility_is_confirmed_before_analysis_and_join_precedes_ack(setup):
    lease, consumer, _, _ = setup

    def ack(receipt):
        assert receipt == "receipt-current"
        assert not lease._thread.is_alive()

    consumer.ack.side_effect = ack
    with message_processing_scope(lease):
        lease.start()
        consumer.renew_visibility.assert_called_once_with("receipt-current", 10800)
        check_message_lease()
        assert not message_cancelled()
    assert lease.finish(True)
    assert not lease.finish(True)
    consumer.ack.assert_called_once()
    with pytest.raises(QueueLeaseLostError):
        lease.renew()
    assert consumer.renew_visibility.call_count == 1


def test_legitimate_processing_beyond_three_hours_keeps_renewing_without_response_timer(setup):
    lease, consumer, clock, _ = setup
    lease.renew()
    for elapsed in [3600, 7200, 10800, 14400, 18000, 21600, 25200, 28800, 32400, 36000]:
        clock.now = 100 + elapsed
        lease.renew()
        assert not lease.is_set()
    assert consumer.renew_visibility.call_count == 11
    assert lease.cap_deadline == 43300
    assert lease.finish(True)


def test_visibility_is_clamped_to_original_receive_cap_and_never_rebased(setup):
    lease, consumer, clock, _ = setup
    lease.renew()
    for elapsed in range(3600, 39601, 3600):
        clock.now = 100 + elapsed
        lease.renew()
    assert consumer.renew_visibility.call_args.args == ("receipt-current", 3534)
    clock.now = 43280
    with pytest.raises(QueueLeaseLostError):
        lease.renew()
    assert lease.cancelled.is_set()
    assert not lease.finish(True)
    consumer.ack.assert_not_called()


@pytest.mark.parametrize("initial", [False, True])
def test_renewal_uncertainty_cancels_only_this_message_and_never_acks(setup, initial):
    lease, consumer, _, shutdown = setup
    if not initial:
        lease.renew()
    consumer.renew_visibility.side_effect = RuntimeError("network failed")
    with pytest.raises(QueueLeaseLostError):
        lease.start() if initial else lease.renew()
    assert lease.is_set() and not shutdown.is_set()
    assert not lease.finish(True)
    consumer.ack.assert_not_called()


def test_pending_renewal_is_joined_before_ack_and_cannot_run_after_ack(setup):
    lease, consumer, _, _ = setup
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def renew(*args):
        calls.append("renew")
        if len(calls) == 2:
            entered.set()
            assert release.wait(2)
            calls.append("returned")

    consumer.renew_visibility.side_effect = renew
    lease.heartbeat_seconds = 0.005
    lease.start()
    assert entered.wait(2)

    def finish():
        lease.finish(True)
        finished.set()

    worker = threading.Thread(target=finish)
    worker.start()
    assert not finished.wait(0.02)
    consumer.ack.assert_not_called()
    release.set()
    worker.join(2)
    assert finished.is_set() and not lease._thread.is_alive()
    assert calls == ["renew", "renew", "returned"]
    consumer.ack.assert_called_once()


def test_late_renewal_success_after_old_visibility_expired_is_not_trusted(setup):
    lease, consumer, clock, _ = setup

    def late(*args):
        clock.now += 10801

    consumer.renew_visibility.side_effect = late
    with pytest.raises(QueueLeaseLostError):
        lease.renew()
    assert not lease.finish(True)


def test_claim_loss_stops_renewal_and_final_ack(setup):
    lease, consumer, _, _ = setup
    owner = MagicMock(return_value=True)
    lease._owner_check = owner
    lease.renew()
    owner.return_value = False
    with pytest.raises(QueueLeaseLostError):
        lease.renew()
    assert lease.is_set() and not lease.finish(True)
    assert consumer.renew_visibility.call_count == 1


def test_owner_unavailable_at_finish_prevents_ack_after_join(setup):
    lease, consumer, _, _ = setup
    owner = MagicMock(return_value=True)
    lease._owner_check = owner
    lease.start()
    owner.side_effect = RuntimeError("DDB unavailable")
    with pytest.raises(QueueLeaseLostError):
        lease.finish(True)
    assert not lease._thread.is_alive()
    consumer.ack.assert_not_called()


def test_captured_check_rejects_active_call_and_late_write_after_loss(setup):
    lease, consumer, _, _ = setup
    with message_processing_scope(lease):
        lease.start()
        captured = get_message_cancellation_check()
        lease._lose("renewal uncertain")
        errors = []

        def response_thread():
            try:
                captured()
            except QueueLeaseLostError:
                errors.append("cancel requested")

        t = threading.Thread(target=response_thread)
        t.start()
        t.join(2)
        assert errors == ["cancel requested"] and message_cancelled()
        with pytest.raises(QueueLeaseLostError):
            check_message_lease()
    assert not lease.finish(True)
    with pytest.raises(QueueLeaseLostError):
        captured()
    check_message_lease()  # unrelated work has no old receipt context
    assert not message_cancelled()


@pytest.mark.parametrize("processed", [False, True])
def test_shutdown_or_expiry_never_acknowledges(setup, processed):
    lease, consumer, clock, shutdown = setup
    lease.start()
    shutdown.set()
    clock.now += 10800
    assert not lease.finish(processed)
    consumer.ack.assert_not_called()


def test_delete_uncertainty_is_not_retried_as_a_second_ack(setup):
    lease, consumer, _, _ = setup
    lease.start()
    consumer.ack.side_effect = RuntimeError("delete ambiguous")
    with pytest.raises(RuntimeError):
        lease.finish(True)
    assert not lease.finish(True)
    consumer.ack.assert_called_once()


def test_unclaimed_or_already_terminal_delivery_does_not_start_renewal():
    clock, consumer = Clock(), MagicMock()
    lease = MessageLease(consumer, QueueMessage({}, "receipt", 1, "message", clock()), threading.Event(), clock=clock)
    with message_processing_scope(lease):
        check_message_lease()
        consumer.renew_visibility.assert_not_called()
        assert lease._thread is None
        with pytest.raises(QueueLeaseLostError, match="current claim"):
            lease.start()
    assert lease.finish(True)
    consumer.renew_visibility.assert_not_called()
    consumer.ack.assert_called_once()


@pytest.mark.parametrize("change", ["", "token", "message", "count", "cancelled", "missing"])
def test_acquired_claim_identity_is_checked_before_first_renewal(change):
    from rca_agent.utils.message_lease import bind_message_claim

    clock, consumer, ddb = Clock(), MagicMock(), MagicMock()
    item = {
        "claim_token": {"S": "owned"},
        "message_id": {"S": "message"},
        "receive_count": {"N": "2"},
        "state": {"S": "SCOPING"},
    }
    if change == "token":
        item["claim_token"]["S"] = "other"
    if change == "message":
        item["message_id"]["S"] = "other"
    if change == "count":
        item["receive_count"]["N"] = "3"
    if change == "cancelled":
        item["state"]["S"] = "CANCELLED"
    ddb.get_item.return_value = {"Item": item} if change != "missing" else {}
    lease = MessageLease(consumer, QueueMessage({}, "receipt", 2, "message", clock()), threading.Event(), clock=clock)
    with message_processing_scope(lease):
        if change:
            with pytest.raises(QueueLeaseLostError):
                bind_message_claim("rca", "owned", dynamodb_client=ddb, table_name="sessions")
            consumer.renew_visibility.assert_not_called()
            assert lease.is_set()
        else:
            bind_message_claim("rca", "owned", dynamodb_client=ddb, table_name="sessions")
            consumer.renew_visibility.assert_called_once_with("receipt", 10800)
            assert ddb.get_item.call_args.kwargs["ConsistentRead"] is True
    assert lease.finish(not change) is (not change)


def test_terminal_transition_stops_background_but_same_claim_can_ack(setup):
    from rca_agent.utils.message_lease import stop_message_renewal

    lease, consumer, _, _ = setup
    owner = MagicMock(return_value=True)
    lease._owner_check = owner
    with message_processing_scope(lease):
        lease.start()
        owner.return_value = "COMPLETED"
        stop_message_renewal()
        assert not lease._thread.is_alive()
        check_message_lease()
    assert lease.finish(True)
    consumer.renew_visibility.assert_called_once()


def test_active_invocation_cancel_request_observes_receipt_failure(setup, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from rca_agent.utils.agent_invocation import invocation_scope, invoke_agent

    lease, consumer, _, _ = setup
    monkeypatch.setattr("rca_agent.utils.agent_invocation._CONTROL_POLL_SECONDS", 0.005)
    started, cancelled = threading.Event(), threading.Event()

    class Agent:
        async def invoke_async(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                return SimpleNamespace(structured_output="late result must not publish")

    def lose():
        assert started.wait(2)
        lease._lose("renewal failed")

    worker = threading.Thread(target=lose)
    with message_processing_scope(lease), invocation_scope(control=lease.check):
        lease.start()
        worker.start()
        with pytest.raises(QueueLeaseLostError):
            invoke_agent(Agent(), "prompt", str, 60)
        worker.join(2)
    assert cancelled.is_set()
    assert not lease.finish(True)
    consumer.ack.assert_not_called()


def test_actual_sdk_request_guard_blocks_late_persistence_and_is_removed_for_next_message(setup):
    from types import SimpleNamespace
    from unittest.mock import patch

    import boto3
    from botocore.awsrequest import AWSResponse

    from rca_agent.utils.message_lease import guard_message_aws_calls

    lease, _, _, _ = setup
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
    response = AWSResponse("https://s3.test", 200, {"content-length": "0"}, SimpleNamespace(stream=lambda: iter([b""])))
    with patch.object(client._endpoint.http_session, "send", return_value=response) as send:
        with message_processing_scope(lease), guard_message_aws_calls(lease, [client]):
            client.put_object(Bucket="review-bucket", Key="before", Body=b"")
            assert send.call_count == 1
            lease._lose("renewal failed while prior model/read completed")
            with pytest.raises(QueueLeaseLostError):
                client.put_object(Bucket="review-bucket", Key="late", Body=b"")
            assert send.call_count == 1
        # The client belongs to the poller and can be reused by the next message.
        client.put_object(Bucket="review-bucket", Key="next", Body=b"")
        assert send.call_count == 2


def test_failed_heartbeat_thread_start_clears_message_context(setup, monkeypatch):
    lease, consumer, _, _ = setup
    monkeypatch.setattr(threading.Thread, "start", MagicMock(side_effect=RuntimeError("thread unavailable")))
    with pytest.raises(RuntimeError), message_processing_scope(lease):
        lease.start()
    assert not message_cancelled()
    assert lease.cancelled.is_set()
    assert not lease.finish(True)
    consumer.ack.assert_not_called()
