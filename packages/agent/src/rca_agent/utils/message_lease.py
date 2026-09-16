"""Keep a single SQS receipt owned independently of a model response's running time."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, RLock, Thread

from rca_agent.config.aws_sdk import AWS_SDK_CALL_WORST_CASE_SECONDS
from rca_agent.ports.interfaces.queue_consumer import QueueConsumerPort, QueueLeaseLostError, QueueMessage

_current_lease: ContextVar[MessageLease | None] = ContextVar("rca_message_lease", default=None)
_VISIBILITY_SECONDS = 10800
_RECEIVE_CAP_SECONDS = 43200
logger = logging.getLogger(__name__)


def check_message_lease() -> None:
    """Check the current receipt before new work or writes; non-queue callers remain unchanged."""
    lease = _current_lease.get()
    if lease is not None:
        lease.check()


def message_cancelled() -> bool:
    """Let active model responses observe message-local cancellation without stopping the poller."""
    lease = _current_lease.get()
    return lease is not None and lease.is_set()


def get_message_cancellation_check() -> Callable[[], None]:
    """Capture the receipt check before entering SDK threads that do not copy ContextVars."""
    lease = _current_lease.get()
    return lease.check if lease is not None else check_message_lease


def bind_message_ownership(check: Callable[[], object]) -> None:
    """Attach the existing claim check after acquisition; no replacement claim is created here."""
    lease = _current_lease.get()
    if lease is not None:
        lease.bind_ownership(check)
        lease.start()


def bind_message_claim(rca_id: str, claim_token: str, *, dynamodb_client, table_name: str) -> None:
    """Bind the acquired token and receipt identity using an existing consistent session read."""
    lease = _current_lease.get()
    if lease is None:
        return

    def check_claim():
        """Distinguish same-token completion from cancellation, another delivery or unknown state."""
        if not dynamodb_client or not table_name or not claim_token:
            raise QueueLeaseLostError("claim ownership cannot be checked")
        response = dynamodb_client.get_item(
            TableName=table_name,
            Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": "ANALYSIS#SESSION"}},
            ConsistentRead=True,
            ProjectionExpression="#st, claim_token, message_id, receive_count",
            ExpressionAttributeNames={"#st": "state"},
        )
        item = response.get("Item", {})
        if (
            item.get("claim_token", {}).get("S") != claim_token
            or item.get("message_id", {}).get("S") != lease.message.message_id
            or item.get("receive_count", {}).get("N") != str(lease.message.receive_count)
        ):
            raise QueueLeaseLostError("message claim token or delivery identity changed")
        state = item.get("state", {}).get("S")
        if state == "COMPLETED":
            return "COMPLETED"
        if state not in {
            "ALARM_RECEIVED",
            "SCOPING",
            "HYPOTHESIS_GENERATION",
            "HYPOTHESIS_PRIORITIZATION",
            "EVIDENCE_COLLECTION",
            "HYPOTHESIS_VALIDATION",
            "REPORT_GENERATION",
        }:
            raise QueueLeaseLostError("claim is cancelled, terminal or unavailable")
        return True

    bind_message_ownership(check_claim)


def stop_message_renewal() -> None:
    """Stop and join renewal as soon as a conditional completion is recorded, before final ACK."""
    lease = _current_lease.get()
    if lease is not None:
        lease.stop_renewal()


@contextmanager
def message_processing_scope(lease: MessageLease):
    """Keep receipt cancellation local; renewal starts only when an actual claim is bound."""
    lease.check()
    token = _current_lease.set(lease)
    try:
        yield lease
    finally:
        try:
            lease.close()
        finally:
            _current_lease.reset(token)


@contextmanager
def guard_message_aws_calls(lease: MessageLease, clients):
    """Fence each actual persistence SDK request, including calls made after an internal read.

    These application clients belong to the single-message processing loop.
    Capturing the lease also covers SDK threads without ContextVar propagation.
    An already sent request keeps its existing claim/side-effect fencing.
    """
    registered = []

    def before_send(**kwargs):
        """Reject the next HTTP attempt after uncertainty without replacing conditional writes."""
        lease.check()

    try:
        for client in clients:
            if client is None:
                continue
            events = client.meta.events
            events.register("before-send.*.*", before_send)
            registered.append(events)
        yield
    finally:
        lease.stop_renewal()
        for events in reversed(registered):
            events.unregister("before-send.*.*", before_send)


class MessageLease:
    """Own one heartbeat and serialize its completion with the final receipt acknowledgement."""

    def __init__(
        self,
        consumer: QueueConsumerPort,
        message: QueueMessage,
        shutdown_event: Event,
        *,
        clock: Callable[[], float] = time.monotonic,
        heartbeat_seconds: float = 60,
        request_bound: float = AWS_SDK_CALL_WORST_CASE_SECONDS,
    ):
        """Use the receive-request anchor, never a renewed or redelivered message's old timestamp."""
        if not math.isfinite(heartbeat_seconds) or heartbeat_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        if not math.isfinite(request_bound) or request_bound <= 0:
            raise ValueError("queue request bound must be positive")
        now = clock()
        if not math.isfinite(message.received_at_monotonic) or not now - message.received_at_monotonic >= 0:
            raise QueueLeaseLostError("receive-request clock is unavailable")
        self.consumer, self.message, self.shutdown_event = consumer, message, shutdown_event
        self.clock, self.heartbeat_seconds, self.request_bound = clock, heartbeat_seconds, request_bound
        self.cap_deadline = message.received_at_monotonic + _RECEIVE_CAP_SECONDS
        self.visibility_deadline = message.received_at_monotonic + _VISIBILITY_SECONDS
        self.cancelled = Event()
        self._stop = Event()
        self._lock = RLock()
        self._thread: Thread | None = None
        self._closed = False
        self._ack_started = False
        self._owner_check: Callable[[], object] | None = None
        self.reason = ""

    def _lose(self, reason: str) -> None:
        """Make uncertainty sticky even if an outstanding request later reports success."""
        if not self.reason:
            logger.warning("Message %s lease lost: %s", self.message.message_id, reason)
        self.reason = self.reason or reason
        self.cancelled.set()
        self._stop.set()

    def is_set(self) -> bool:
        """Serve as the orchestrator's per-message cancellation Event, including local expiry."""
        if self.shutdown_event.is_set():
            self._lose("process shutdown")
        if self.clock() >= min(self.cap_deadline, self.visibility_deadline):
            self._lose("receipt visibility expired")
        return self._closed or self.cancelled.is_set()

    def check(self) -> None:
        """Fence new work after local loss; existing conditional claim writes still provide atomic fencing."""
        if self.is_set():
            raise QueueLeaseLostError(self.reason or "message processing already ended")

    def _check_owner(self) -> None:
        """A false result or unavailable ownership check cannot authorize another renewal or ACK."""
        if self._owner_check is not None:
            try:
                result = self._owner_check()
                if result is False:
                    raise QueueLeaseLostError("claim ownership lost")
                if result == "COMPLETED":
                    self._stop.set()
            except Exception as exc:
                self._lose("claim ownership unavailable or lost")
                raise QueueLeaseLostError(self.reason) from exc

    def bind_ownership(self, check: Callable[[], object]) -> None:
        """Bind once to this receipt's current claim and verify it before continuing analysis."""
        with self._lock:
            self.check()
            if self._owner_check is not None:
                raise QueueLeaseLostError("message claim checker already bound")
            self._owner_check = check
            self._check_owner()
            self.check()
            if self._stop.is_set():
                raise QueueLeaseLostError("claim already completed before processing began")

    def renew(self) -> None:
        """Renew within the original receive cap and reject late success after an expired old lease."""
        with self._lock:
            self.check()
            if self._owner_check is None:
                raise QueueLeaseLostError("a current claim is required before visibility renewal")
            if self._stop.is_set() and not self._closed:
                return
            self._check_owner()
            self.check()
            if self._stop.is_set():
                return
            now = self.clock()
            seconds = min(_VISIBILITY_SECONDS, math.floor(self.cap_deadline - now - self.request_bound - 1))
            if seconds <= self.request_bound or now + self.request_bound >= self.visibility_deadline:
                self._lose("insufficient receipt lifetime to confirm renewal")
                raise QueueLeaseLostError(self.reason)
            try:
                self.consumer.renew_visibility(self.message.receipt_handle, seconds)
                self.check()
                if self.clock() - now > self.request_bound:
                    raise QueueLeaseLostError("renewal exceeded bounded request lifetime")
                self.visibility_deadline = now + seconds
            except Exception as exc:
                self._lose("visibility renewal failed or was not confirmed")
                raise QueueLeaseLostError(self.reason) from exc

    def start(self) -> None:
        """Confirm initial visibility before spawning the only heartbeat and before any analysis."""
        if self._owner_check is None:
            raise QueueLeaseLostError("a current claim is required before visibility renewal")
        if self._thread is not None:
            raise QueueLeaseLostError("receipt heartbeat already started")
        self.renew()
        if self._stop.is_set():
            return
        self._thread = Thread(target=self._heartbeat, name="rca-receipt-heartbeat")
        try:
            self._thread.start()
        except Exception:
            self._thread = None
            self._lose("receipt heartbeat could not start")
            raise

    def _heartbeat(self) -> None:
        """Wait interruptibly and stop permanently after uncertainty, without extending model budgets."""
        while not self._stop.wait(
            min(self.heartbeat_seconds, max(0.001, (self.visibility_deadline - self.clock()) / 3))
        ):
            try:
                self.renew()
            except Exception:
                self._lose("receipt heartbeat stopped")
                return

    def stop_renewal(self) -> None:
        """Join the bounded renewal without invalidating same-token completion publication."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def close(self) -> None:
        """Join outstanding renewal before allowing the caller to reach the final ACK gate."""
        self.stop_renewal()
        self._closed = True

    def finish(self, processed: bool) -> bool:
        """ACK once, only after join and a fresh local/claim check with enough bounded request time."""
        self.close()
        with self._lock:
            if not processed or self._ack_started or self.cancelled.is_set() or self.shutdown_event.is_set():
                return False
            self._check_owner()
            if self.cancelled.is_set() or self.shutdown_event.is_set():
                return False
            if self.clock() + self.request_bound >= min(self.visibility_deadline, self.cap_deadline):
                self._lose("insufficient receipt lifetime for acknowledgement")
                return False
            self._ack_started = True
            try:
                self.consumer.ack(self.message.receipt_handle)
            except Exception:
                self._lose("receipt acknowledgement was not confirmed")
                raise
            return True
