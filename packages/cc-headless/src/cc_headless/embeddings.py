import boto3 as _boto3

from cc_headless.adapters.secondary.embedding.bedrock_embedding import BedrockEmbeddingAdapter  # noqa: F401
from cc_headless.config.settings import BEDROCK_EMBEDDING_MODEL_ID, S3_VECTOR_REGION  # noqa: F401

_bedrock_client = None


def _get_bedrock_client():
    global _bedrock_client  # noqa: PLW0603
    if _bedrock_client is None:
        _bedrock_client = _boto3.client("bedrock-runtime", region_name=S3_VECTOR_REGION)
    return _bedrock_client


def embed_document(text, *, bedrock_client=None):
    client = bedrock_client or _get_bedrock_client()
    adapter = BedrockEmbeddingAdapter(client)
    return adapter.embed_document(text)
