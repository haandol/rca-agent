from __future__ import annotations

import json
import logging

import boto3

from rca_agent.config import BEDROCK_EMBEDDING_MODEL_ID, BEDROCK_REGION

logger = logging.getLogger(__name__)

_bedrock_client = None


def _get_bedrock_client():
    global _bedrock_client  # noqa: PLW0603
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    return _bedrock_client


def embed_texts(
    texts: list[str],
    *,
    input_type: str = "search_document",
    bedrock_client=None,
) -> list[list[float]]:
    client = bedrock_client or _get_bedrock_client()
    response = client.invoke_model(
        modelId=BEDROCK_EMBEDDING_MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(
            {
                "texts": texts,
                "input_type": input_type,
                "embedding_types": ["float"],
            }
        ),
    )
    result = json.loads(response["body"].read())
    return result["embeddings"]["float"]


def embed_query(text: str, *, bedrock_client=None) -> list[float]:
    return embed_texts([text], input_type="search_query", bedrock_client=bedrock_client)[0]


def embed_document(text: str, *, bedrock_client=None) -> list[float]:
    return embed_texts([text], input_type="search_document", bedrock_client=bedrock_client)[0]
