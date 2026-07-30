"""플레이북 실행 워커의 진입점.

승인이 곧 메시지다. 이 워커는 사용자가 발행한 실행 요청만 소비하며, 분석 완료 이벤트를
구독하지 않는다 — 승인 없이 실행이 기동될 경로를 두지 않는 것이 이 구조의 목적이다.
"""

from __future__ import annotations

import signal
import sys
import time
from threading import Event

import boto3
import structlog

from cc_headless.adapters.primary.health_server import start_health_server
from cc_headless.config.settings import (
    EXECUTION_POLL_WAIT_SECONDS,
    EXECUTION_QUEUE_URL,
)
from cc_headless.di.execution_container import AppExecutionContainer
from cc_headless.logging import setup_logging
from cc_headless.services.execution_pipeline import ExecutionOrchestrator

logger = structlog.get_logger()


def main() -> None:
    setup_logging()

    if not EXECUTION_QUEUE_URL:
        logger.error("execution_queue_url_missing")
        sys.exit(1)

    start_health_server()
    logger.info("health_server_started", port=8080)

    container = AppExecutionContainer()
    shutdown_event = Event()
    orchestrator = ExecutionOrchestrator(container, shutdown_event=shutdown_event)

    def _handle_signal(signum, _frame):
        logger.info("shutdown_signal_received", signal=signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    sqs = boto3.client("sqs")
    logger.info("execution_polling_started", queue_url=EXECUTION_QUEUE_URL)

    while not shutdown_event.is_set():
        try:
            resp = sqs.receive_message(
                QueueUrl=EXECUTION_QUEUE_URL,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=EXECUTION_POLL_WAIT_SECONDS,
                AttributeNames=["ApproximateReceiveCount"],
            )
        except Exception:
            logger.exception("execution_sqs_receive_failed")
            time.sleep(5)
            continue

        for msg in resp.get("Messages", []):
            try:
                success = orchestrator.process_message(msg.get("Body", "{}"))
            except Exception:
                logger.exception("execution_message_processing_failed")
                success = False

            if success:
                sqs.delete_message(QueueUrl=EXECUTION_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])

    logger.info("shutdown_complete")


if __name__ == "__main__":
    main()
