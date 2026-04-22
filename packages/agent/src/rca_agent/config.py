from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / "env" / "local.env", override=False)

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-6")
BEDROCK_HAIKU_MODEL_ID = os.environ.get("BEDROCK_HAIKU_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
BEDROCK_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MAX_TOKENS = int(os.environ.get("BEDROCK_MAX_TOKENS", "4096"))
BEDROCK_HAIKU_MAX_TOKENS = int(os.environ.get("BEDROCK_HAIKU_MAX_TOKENS", "4096"))

THINKING_ENABLED = os.environ.get("THINKING_ENABLED", "false").lower() in ("true", "1", "yes")

SCOPING_TIMEOUT_SECONDS = int(os.environ.get("SCOPING_TIMEOUT_SECONDS", "300"))
HYPOTHESIS_GENERATION_TIMEOUT_SECONDS = int(os.environ.get("HYPOTHESIS_GENERATION_TIMEOUT_SECONDS", "180"))
HYPOTHESIS_GENERATION_MAX_RETRIES = int(os.environ.get("HYPOTHESIS_GENERATION_MAX_RETRIES", "3"))
LLM_DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("LLM_DEFAULT_TIMEOUT_SECONDS", "120"))
EVIDENCE_COLLECTION_TIMEOUT_SECONDS = int(os.environ.get("EVIDENCE_COLLECTION_TIMEOUT_SECONDS", "120"))

RCA_TIME_BUDGET_SECONDS = int(os.environ.get("RCA_TIME_BUDGET_SECONDS", "1200"))
RCA_MAX_TREE_DEPTH = int(os.environ.get("RCA_MAX_TREE_DEPTH", "5"))
RCA_MAX_VALIDATION_LOOPS = int(os.environ.get("RCA_MAX_VALIDATION_LOOPS", "3"))
RCA_MAX_REGENERATION_ROUNDS = int(os.environ.get("RCA_MAX_REGENERATION_ROUNDS", "2"))
CONFIRMATION_THRESHOLD = float(os.environ.get("CONFIRMATION_THRESHOLD", "0.8"))
REJECTION_THRESHOLD = float(os.environ.get("REJECTION_THRESHOLD", "0.3"))
TERMINATION_CONFIDENCE_THRESHOLD = float(os.environ.get("TERMINATION_CONFIDENCE_THRESHOLD", "0.9"))
MAX_BRANCHING_DEPTH = int(os.environ.get("MAX_BRANCHING_DEPTH", "3"))

PLAYBOOK_SIMILARITY_THRESHOLD = float(os.environ.get("PLAYBOOK_SIMILARITY_THRESHOLD", "0.7"))
PLAYBOOK_UPDATE_THRESHOLD = float(os.environ.get("PLAYBOOK_UPDATE_THRESHOLD", "0.86"))
PLAYBOOK_TOP_K = int(os.environ.get("PLAYBOOK_TOP_K", "3"))

S3_VECTOR_BUCKET_NAME = os.environ.get("S3_VECTOR_BUCKET_NAME", "")
S3_VECTOR_PLAYBOOK_INDEX = os.environ.get("S3_VECTOR_PLAYBOOK_INDEX", "playbook-index")
S3_EVIDENCE_BUCKET = os.environ.get("S3_EVIDENCE_BUCKET", "")
S3_EVIDENCE_MAX_RETRIES = int(os.environ.get("S3_EVIDENCE_MAX_RETRIES", "3"))
S3_REPORT_BUCKET = os.environ.get("S3_REPORT_BUCKET", "")
SNS_NOTIFICATION_TOPIC_ARN = os.environ.get("SNS_NOTIFICATION_TOPIC_ARN", "")

DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "")
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "90"))
