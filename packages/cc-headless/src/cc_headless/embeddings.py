from __future__ import annotations

import json

import boto3
import structlog

from cc_headless.config import BEDROCK_EMBEDDING_MODEL_ID

logger = structlog.get_logger()

_bedrock_client = None


def _get_bedrock_client():
    global _bedrock_client  # noqa: PLW0603
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime")
    return _bedrock_client


def embed_document(text: str, *, bedrock_client=None) -> list[float]:
    client = bedrock_client or _get_bedrock_client()
    response = client.invoke_model(
        modelId=BEDROCK_EMBEDDING_MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(
            {
                "texts": [text],
                "input_type": "search_document",
                "embedding_types": ["float"],
            }
        ),
    )
    result = json.loads(response["body"].read())
    return result["embeddings"]["float"][0]
