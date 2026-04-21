from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time

import boto3

from rca_agent.agent_factory import create_cloudwatch_mcp_client, create_scoping_agent
from rca_agent.config import S3_VECTOR_BUCKET_NAME
from rca_agent.healthz import start_health_server
from rca_agent.models import AlarmPayload
from rca_agent.scoping import run_scoping

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


def _parse_sns_envelope(body: dict) -> dict:
    """Extract the CloudWatch alarm payload from an SNS envelope.

    SQS messages from an SNS subscription wrap the actual payload inside a
    top-level "Message" field (JSON-encoded string). If the body is already
    a raw CloudWatch alarm payload (e.g. in tests), return it as-is.
    """
    if "Message" in body and isinstance(body["Message"], str):
        return json.loads(body["Message"])
    return body


def _create_s3_vectors_client():
    if not S3_VECTOR_BUCKET_NAME:
        return None
    return boto3.client("s3vectors")


def _process_alarm(body: dict, agent, s3_vectors_client) -> None:
    alarm_data = _parse_sns_envelope(body)
    alarm = AlarmPayload.from_cloudwatch_sns(alarm_data)
    logger.info(
        "Parsed alarm: name=%s, resource=%s, service=%s",
        alarm.alarm_name,
        alarm.resource_id,
        alarm.service_name,
    )

    scoping_result = run_scoping(alarm, agent, s3_vectors_client=s3_vectors_client)
    logger.info(
        "Scoping result: severity=%s, blast_radius=%s, playbooks=%d",
        scoping_result.initial_severity,
        scoping_result.blast_radius,
        len(scoping_result.similar_playbooks),
    )
    # TODO: pass scoping_result to hypothesis generation (ADR 0002)


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if not QUEUE_URL:
        logger.error("SQS_QUEUE_URL is not set")
        sys.exit(1)

    start_health_server()
    logger.info("Health server started on port 8000")

    mcp_client = create_cloudwatch_mcp_client()
    agent = create_scoping_agent(mcp_clients=[mcp_client])
    s3_vectors_client = _create_s3_vectors_client()
    logger.info("Scoping agent initialized")

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
                logger.info("Received alarm message: %s", body.get("AlarmName", body.get("Message", "unknown")[:80]))
                _process_alarm(body, agent, s3_vectors_client)
            except Exception:
                logger.exception("Failed to process message")
            finally:
                sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])

    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
