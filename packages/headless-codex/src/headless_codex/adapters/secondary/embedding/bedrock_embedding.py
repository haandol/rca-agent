from __future__ import annotations

import json

from headless_codex.config.settings import BEDROCK_EMBEDDING_MODEL_ID
from headless_codex.ports.interfaces.embedding import EmbeddingPort


class BedrockEmbeddingAdapter(EmbeddingPort):
    def __init__(self, bedrock_client=None):
        self._client = bedrock_client

    def _embed(self, text: str, *, input_type: str) -> list[float]:
        response = self._client.invoke_model(
            modelId=BEDROCK_EMBEDDING_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(
                {
                    "texts": [text],
                    "input_type": input_type,
                    "embedding_types": ["float"],
                }
            ),
        )
        result = json.loads(response["body"].read())
        return result["embeddings"]["float"][0]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text, input_type="search_query")

    def embed_document(self, text: str) -> list[float]:
        return self._embed(text, input_type="search_document")
