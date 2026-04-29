from __future__ import annotations

import logging
import os
import signal
import sys
from threading import Event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
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

    container = AppContainer(queue_url, poll_wait_seconds=poll_wait)
    shutdown_event = Event()
    orchestrator = PipelineOrchestrator(container, shutdown_event=shutdown_event)
    consumer = container.queue_consumer

    def _handle_signal(signum, _frame):
        logger.info("Received signal %s, shutting down", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Pipeline initialized")
    logger.info("Starting SQS long polling: %s", queue_url)

    while not shutdown_event.is_set():
        for body, receipt_handle in consumer.poll():
            try:
                orchestrator.process_alarm(body)
            except Exception:
                logger.exception("Failed to process message")
            finally:
                consumer.ack(receipt_handle)

    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
