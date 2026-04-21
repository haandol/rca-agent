from __future__ import annotations

import os

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")
BEDROCK_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MAX_TOKENS = int(os.environ.get("BEDROCK_MAX_TOKENS", "4096"))

SCOPING_TIMEOUT_SECONDS = int(os.environ.get("SCOPING_TIMEOUT_SECONDS", "300"))
PLAYBOOK_SIMILARITY_THRESHOLD = float(os.environ.get("PLAYBOOK_SIMILARITY_THRESHOLD", "0.7"))
PLAYBOOK_TOP_K = int(os.environ.get("PLAYBOOK_TOP_K", "3"))

S3_VECTOR_BUCKET_NAME = os.environ.get("S3_VECTOR_BUCKET_NAME", "")
S3_VECTOR_PLAYBOOK_INDEX = os.environ.get("S3_VECTOR_PLAYBOOK_INDEX", "playbook-index")
