from __future__ import annotations

import json

from cc_headless.config.settings import BEDROCK_EMBEDDING_MODEL_ID
from cc_headless.ports.interfaces.embedding import EmbeddingPort


class BedrockEmbeddingAdapter(EmbeddingPort):
    def __init__(self, bedrock_client=None):
        self._client = bedrock_client

    def embed_document(self, text: str) -> list[float]:
        response = self._client.invoke_model(
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
