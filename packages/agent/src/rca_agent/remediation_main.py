from __future__ import annotations

import json
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


def _parse_sns_envelope(body: dict) -> dict:
    """SQS delivers either a raw message or an SNS envelope. Return the RCA
    notification payload as a dict either way."""
    if "Message" in body and "TopicArn" in body:
        try:
            return json.loads(body["Message"])
        except (json.JSONDecodeError, TypeError):
            return {}
    return body


def main() -> None:
    queue_url = os.environ.get("REMEDIATION_QUEUE_URL", "")
    poll_wait = int(os.environ.get("REMEDIATION_SQS_POLL_WAIT_SECONDS", "20"))
    if not queue_url:
        logger.error("REMEDIATION_QUEUE_URL is not set")
        sys.exit(1)

    from rca_agent.adapters.primary.health.health_server import start_health_server

    start_health_server()
    logger.info("Health server started on port 8000")

    from rca_agent.di.app_container import AppContainer
    from rca_agent.services.remediation_pipeline import RemediationOrchestrator

    container = AppContainer(queue_url, poll_wait_seconds=poll_wait)
    orchestrator = RemediationOrchestrator(container)
    consumer = container.queue_consumer

    shutdown_event = Event()

    def _handle_signal(signum, _frame):
        logger.info("Received signal %s, shutting down", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Remediation agent initialized")
    logger.info("Starting SQS long polling: %s", queue_url)

    while not shutdown_event.is_set():
        for body, receipt_handle in consumer.poll():
            try:
                payload = _parse_sns_envelope(body)
                if payload:
                    orchestrator.process_notification(payload)
                else:
                    logger.warning("Empty or unparseable notification, skipping")
            except Exception:
                logger.exception("Failed to process remediation message")
            finally:
                consumer.ack(receipt_handle)

    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
