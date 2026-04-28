from __future__ import annotations

import logging
import os
import signal
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

_running = True


def _handle_signal(signum, _frame):
    global _running  # noqa: PLW0603
    logger.info("Received signal %s, shutting down", signum)
    _running = False


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

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
    orchestrator = PipelineOrchestrator(container)
    consumer = container.queue_consumer
    logger.info("Pipeline initialized")
    logger.info("Starting SQS long polling: %s", queue_url)

    while _running:
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
