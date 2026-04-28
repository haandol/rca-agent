from __future__ import annotations

import os

SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
SQS_POLL_WAIT_SECONDS = int(os.environ.get("SQS_POLL_WAIT_SECONDS", "20"))

DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "")
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "90"))

S3_REPORT_BUCKET = os.environ.get("S3_REPORT_BUCKET", "")
SNS_NOTIFICATION_TOPIC_ARN = os.environ.get("SNS_NOTIFICATION_TOPIC_ARN", "")
S3_VECTOR_BUCKET_NAME = os.environ.get("S3_VECTOR_BUCKET_NAME", "")
S3_VECTOR_PLAYBOOK_INDEX = os.environ.get("S3_VECTOR_PLAYBOOK_INDEX", "playbook")
S3_VECTOR_REGION = os.environ.get("S3_VECTOR_REGION", "us-east-1")
BEDROCK_EMBEDDING_MODEL_ID = os.environ.get("BEDROCK_EMBEDDING_MODEL_ID", "cohere.embed-v4:0")

PRESIGNED_URL_EXPIRY = 86400

CC_TIMEOUT_SECONDS = int(os.environ.get("CC_TIMEOUT_SECONDS", "1800"))

ALARM_STALENESS_SECONDS = int(os.environ.get("ALARM_STALENESS_SECONDS", "1800"))

ENGINE = "cc-headless"
