from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time

import boto3

from rca_agent.healthz import start_health_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
POLL_WAIT_SECONDS = int(os.environ.get("SQS_POLL_WAIT_SECONDS", "20"))

_running = True


def _handle_signal(signum, _frame):
    global _running  # noqa: PLW0603
    logger.info("Received signal %s, shutting down", signum)
    _running = False


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if not QUEUE_URL:
        logger.error("SQS_QUEUE_URL is not set")
        sys.exit(1)

    start_health_server()
    logger.info("Health server started on port 8000")

    sqs = boto3.client("sqs")
    logger.info("Starting SQS long polling: %s", QUEUE_URL)

    while _running:
        try:
            resp = sqs.receive_message(
                QueueUrl=QUEUE_URL,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=POLL_WAIT_SECONDS,
            )
        except Exception:
            logger.exception("Failed to receive SQS message")
            time.sleep(5)
            continue

        messages = resp.get("Messages", [])
        if not messages:
            continue

        for msg in messages:
            try:
                body = json.loads(msg["Body"])
                logger.info("Received alarm message: %s", body.get("AlarmName", "unknown"))
                # TODO: parse AlarmPayload, run scoping, hypothesis loop
            except Exception:
                logger.exception("Failed to process message")
            finally:
                sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])

    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
