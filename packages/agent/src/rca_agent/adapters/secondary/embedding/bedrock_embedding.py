from __future__ import annotations

import json
import logging

import boto3

from rca_agent.config.settings import BEDROCK_EMBEDDING_MODEL_ID, BEDROCK_REGION
from rca_agent.ports.interfaces.embedding import EmbeddingPort

logger = logging.getLogger(__name__)


class BedrockEmbeddingAdapter(EmbeddingPort):
    def __init__(self, bedrock_client=None):
        self._client = bedrock_client

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
        return self._client

    def _embed_texts(self, texts: list[str], *, input_type: str = "search_document") -> list[list[float]]:
        response = self.client.invoke_model(
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

    def embed_query(self, text: str) -> list[float]:
        return self._embed_texts([text], input_type="search_query")[0]

    def embed_document(self, text: str) -> list[float]:
        return self._embed_texts([text], input_type="search_document")[0]
