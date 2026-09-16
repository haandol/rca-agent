from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator

import boto3

from rca_agent.config.aws_sdk import SIDE_EFFECT_AWS_CLIENT_CONFIG
from rca_agent.ports.interfaces.queue_consumer import (
    QueueConsumerPort,
    QueueLeaseLostError,
    QueueMessage,
    QueueReceiveError,
)

logger = logging.getLogger(__name__)


class SqsConsumer(QueueConsumerPort):
    def __init__(self, queue_url: str, *, poll_wait_seconds: int = 20):
        """Bound queue requests so a receipt heartbeat can be stopped and joined before ACK."""
        self._queue_url = queue_url
        self._poll_wait = poll_wait_seconds
        self._sqs = boto3.client("sqs", config=SIDE_EFFECT_AWS_CLIENT_CONFIG)

    def poll(self) -> Iterator[QueueMessage]:
        """Anchor each receipt before receiving, including long-poll and network elapsed time."""
        received_at = time.monotonic()
        try:
            resp = self._sqs.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=self._poll_wait,
                AttributeNames=["ApproximateReceiveCount"],
                VisibilityTimeout=10800,
            )
        except Exception as exc:
            raise QueueReceiveError("Failed to receive SQS message") from exc

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
            yield QueueMessage(body, msg["ReceiptHandle"], receive_count, message_id, received_at)

    def ack(self, receipt_handle: str) -> None:
        """Delete only the receipt selected by the processing owner's final ACK gate."""
        self._sqs.delete_message(
            QueueUrl=self._queue_url,
            ReceiptHandle=receipt_handle,
        )

    def renew_visibility(self, receipt_handle: str, visibility_seconds: int) -> None:
        """Fail closed on an expired receipt, transport failure or unconfirmed SDK response."""
        if type(visibility_seconds) is not int or not 1 <= visibility_seconds <= 43200:
            raise QueueLeaseLostError("invalid receipt visibility duration")
        try:
            response = self._sqs.change_message_visibility(
                QueueUrl=self._queue_url,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=visibility_seconds,
            )
            if response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200:
                raise QueueLeaseLostError("visibility renewal was not confirmed")
        except Exception as exc:
            raise QueueLeaseLostError("could not renew the current SQS receipt") from exc
