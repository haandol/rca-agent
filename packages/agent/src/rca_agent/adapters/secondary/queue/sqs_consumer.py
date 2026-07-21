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

    def poll(self) -> Iterator[tuple[dict, str, int, str]]:
        try:
            resp = self._sqs.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=self._poll_wait,
                AttributeNames=["ApproximateReceiveCount"],
            )
        except Exception:
            logger.exception("Failed to receive SQS message")
            return

        for msg in resp.get("Messages", []):
            message_id = msg.get("MessageId")
            if not message_id:
                logger.error("SQS message is missing MessageId; leaving it unacknowledged")
                continue
            body = json.loads(msg["Body"])
            raw_receive_count = msg.get("Attributes", {}).get("ApproximateReceiveCount", "1")
            try:
                receive_count = max(int(raw_receive_count), 1)
            except (TypeError, ValueError):
                logger.warning("Invalid ApproximateReceiveCount %r; defaulting to 1", raw_receive_count)
                receive_count = 1
            yield body, msg["ReceiptHandle"], receive_count, message_id

    def ack(self, receipt_handle: str) -> None:
        self._sqs.delete_message(
            QueueUrl=self._queue_url,
            ReceiptHandle=receipt_handle,
        )
