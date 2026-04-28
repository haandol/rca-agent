from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import boto3

from rca_agent.ports.interfaces.queue_consumer import QueueConsumerPort

logger = logging.getLogger(__name__)


class SqsConsumer(QueueConsumerPort):
    def __init__(self, queue_url: str, *, poll_wait_seconds: int = 20):
        self._queue_url = queue_url
        self._poll_wait = poll_wait_seconds
        self._sqs = boto3.client("sqs")

    def poll(self) -> Iterator[tuple[dict, str]]:
        try:
            resp = self._sqs.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=self._poll_wait,
            )
        except Exception:
            logger.exception("Failed to receive SQS message")
            return

        for msg in resp.get("Messages", []):
            body = json.loads(msg["Body"])
            yield body, msg["ReceiptHandle"]

    def ack(self, receipt_handle: str) -> None:
        self._sqs.delete_message(
            QueueUrl=self._queue_url,
            ReceiptHandle=receipt_handle,
        )
