from __future__ import annotations

import signal
import sys
import time

import boto3
import structlog

from cc_headless.config.settings import (
    SQS_POLL_WAIT_SECONDS,
    SQS_QUEUE_URL,
)
from cc_headless.di.app_container import AppContainer
from cc_headless.healthz import start_health_server
from cc_headless.logging import setup_logging
from cc_headless.services.pipeline import PipelineOrchestrator

logger = structlog.get_logger()

_running = True


def _handle_signal(signum, _frame):
    global _running  # noqa: PLW0603
    logger.info("shutdown_signal_received", signal=signum)
    _running = False


def main() -> None:
    setup_logging()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if not SQS_QUEUE_URL:
        logger.error("sqs_queue_url_missing")
        sys.exit(1)

    start_health_server()
    logger.info("health_server_started", port=8080)

    container = AppContainer()
    orchestrator = PipelineOrchestrator(container)

    sqs = boto3.client("sqs")
    logger.info("sqs_polling_started", queue_url=SQS_QUEUE_URL)

    while _running:
        try:
            resp = sqs.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=SQS_POLL_WAIT_SECONDS,
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
                success = orchestrator.process_message(msg.get("Body", "{}"))
            except Exception:
                logger.exception("message_processing_failed")
                success = False

            if success:
                sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])

    logger.info("shutdown_complete")


if __name__ == "__main__":
    main()
