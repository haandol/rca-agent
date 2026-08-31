from __future__ import annotations

import signal
import sys
import time
from threading import Event

import boto3
import structlog

from headless_codex.adapters.primary.health_server import start_health_server
from headless_codex.config.settings import (
    SQS_POLL_WAIT_SECONDS,
    SQS_QUEUE_URL,
)
from headless_codex.di.app_container import AppContainer
from headless_codex.logging import setup_logging
from headless_codex.services.pipeline import PipelineOrchestrator

logger = structlog.get_logger()


def main() -> None:
    setup_logging()

    if not SQS_QUEUE_URL:
        logger.error("sqs_queue_url_missing")
        sys.exit(1)

    start_health_server()
    logger.info("health_server_started", port=8080)

    container = AppContainer()
    shutdown_event = Event()
    orchestrator = PipelineOrchestrator(container, shutdown_event=shutdown_event)

    def _handle_signal(signum, _frame):
        logger.info("shutdown_signal_received", signal=signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    sqs = boto3.client("sqs")
    logger.info("sqs_polling_started", queue_url=SQS_QUEUE_URL)

    while not shutdown_event.is_set():
        try:
            resp = sqs.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=SQS_POLL_WAIT_SECONDS,
                AttributeNames=["ApproximateReceiveCount"],
            )
        except Exception:
            logger.exception("sqs_receive_failed")
            time.sleep(5)
            continue

        messages = resp.get("Messages", [])
        if not messages:
            continue

        for msg in messages:
            try:
                receive_count = int(msg.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
                success = orchestrator.process_message(
                    msg.get("Body", "{}"),
                    receive_count=receive_count,
                )
            except Exception:
                logger.exception("message_processing_failed")
                success = False

            if success:
                sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])

    logger.info("shutdown_complete")


if __name__ == "__main__":
    main()
