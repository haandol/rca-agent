from __future__ import annotations

import logging
import os
import signal
import sys
from threading import Event

from rca_agent.ports.interfaces.queue_consumer import QueueReceiveError
from rca_agent.utils.message_lease import MessageLease, guard_message_aws_calls, message_processing_scope

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
_INITIAL_RECEIVE_RETRY_SECONDS = 1.0
_MAX_RECEIVE_RETRY_SECONDS = 30.0


def main() -> None:
    """Process one owned receipt at a time, joining its heartbeat before success-only ACK."""
    queue_url = os.environ.get("SQS_QUEUE_URL", "")
    poll_wait = int(os.environ.get("SQS_POLL_WAIT_SECONDS", "20"))
    if not queue_url:
        logger.error("SQS_QUEUE_URL is not set")
        sys.exit(1)

    from rca_agent.adapters.primary.health.health_server import start_health_server

    start_health_server()
    logger.info("Health server started on port 8000")

    from rca_agent.di.app_container import AppContainer
    from rca_agent.services.pipeline import PipelineOrchestrator
    from rca_agent.utils.agent_invocation import invocation_scope

    container = AppContainer(queue_url, poll_wait_seconds=poll_wait)
    shutdown_event = Event()
    consumer = container.queue_consumer

    def _handle_signal(signum, _frame):
        logger.info("Received signal %s, shutting down", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Pipeline initialized")
    logger.info("Starting SQS long polling: %s", queue_url)

    retry_seconds = _INITIAL_RECEIVE_RETRY_SECONDS
    while not shutdown_event.is_set():
        try:
            # The consumer receives one message per poll. Materialize its iterator
            # here so a receive failure cannot be confused with processing failure.
            messages = list(consumer.poll())
        except QueueReceiveError:
            logger.exception("Queue receive failed; retrying in %.1fs", retry_seconds)
            shutdown_event.wait(retry_seconds)
            retry_seconds = min(retry_seconds * 2, _MAX_RECEIVE_RETRY_SECONDS)
            continue
        retry_seconds = _INITIAL_RECEIVE_RETRY_SECONDS
        for message in messages:
            if shutdown_event.is_set():
                break
            lease = None
            processed = False
            try:
                lease = MessageLease(consumer, message, shutdown_event)
                with (
                    message_processing_scope(lease),
                    guard_message_aws_calls(
                        lease,
                        (
                            container.dynamodb_client,
                            container.s3_client,
                            container.s3_vectors_client,
                            container.sns_client,
                        ),
                    ),
                    invocation_scope(control=lease.check),
                ):
                    orchestrator = PipelineOrchestrator(container, shutdown_event=lease)
                    processed = orchestrator.process_alarm(
                        message.body,
                        receive_count=message.receive_count,
                        message_id=message.message_id,
                    )
            except Exception:
                logger.exception("Failed to process message")
            finally:
                try:
                    if lease is not None:
                        lease.finish(processed)
                except Exception:
                    logger.exception("Failed to finish owned message")

    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
