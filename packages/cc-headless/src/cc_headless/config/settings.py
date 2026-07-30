from __future__ import annotations

import os

SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
SQS_POLL_WAIT_SECONDS = int(os.environ.get("SQS_POLL_WAIT_SECONDS", "20"))

EXECUTION_QUEUE_URL = os.environ.get("EXECUTION_QUEUE_URL", "")
EXECUTION_POLL_WAIT_SECONDS = int(os.environ.get("EXECUTION_POLL_WAIT_SECONDS", "20"))

DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "")
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "90"))

S3_EVIDENCE_BUCKET = os.environ.get("S3_EVIDENCE_BUCKET", "")
S3_REPORT_BUCKET = os.environ.get("S3_REPORT_BUCKET", "")
SNS_NOTIFICATION_TOPIC_ARN = os.environ.get("SNS_NOTIFICATION_TOPIC_ARN", "")
S3_VECTOR_BUCKET_NAME = os.environ.get("S3_VECTOR_BUCKET_NAME", "")
S3_VECTOR_PLAYBOOK_INDEX = os.environ.get("S3_VECTOR_PLAYBOOK_INDEX", "playbook")
S3_VECTOR_REGION = os.environ.get("S3_VECTOR_REGION", "us-east-1")
BEDROCK_EMBEDDING_MODEL_ID = os.environ.get("BEDROCK_EMBEDDING_MODEL_ID", "cohere.embed-v4:0")

PRESIGNED_URL_EXPIRY = 86400

CC_TIMEOUT_SECONDS = int(os.environ.get("CC_TIMEOUT_SECONDS", "1800"))

# Execution includes waiting for the success criteria to become observable, so it
# gets its own budget rather than reusing the analysis timeout.
EXECUTION_TIMEOUT_SECONDS = int(os.environ.get("EXECUTION_TIMEOUT_SECONDS", "3600"))
RETROSPECTIVE_TIMEOUT_SECONDS = int(os.environ.get("RETROSPECTIVE_TIMEOUT_SECONDS", "900"))

# The claim has to outlive the worst-case execution or an in-flight request gets
# redelivered and runs a second time.
EXECUTION_CLAIM_SECONDS = max(
    int(os.environ.get("EXECUTION_CLAIM_SECONDS", "0")) or EXECUTION_TIMEOUT_SECONDS + 900,
    EXECUTION_TIMEOUT_SECONDS + 900,
)

ALARM_STALENESS_SECONDS = int(os.environ.get("ALARM_STALENESS_SECONDS", "1800"))

# The analysis run holds no side-effect lease of its own beyond the final
# publication, so the lease only has to outlive report/playbook persistence and
# the notification retry.
SIDE_EFFECT_LEASE_SECONDS = min(
    max(int(os.environ.get("SIDE_EFFECT_LEASE_SECONDS", "60")), 60),
    300,
)

ENGINE = "cc-headless"
