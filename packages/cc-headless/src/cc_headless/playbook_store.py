import boto3 as _boto3

from cc_headless.adapters.secondary.embedding.bedrock_embedding import BedrockEmbeddingAdapter  # noqa: F401
from cc_headless.adapters.secondary.playbook.s3_vectors_playbook_store import S3VectorsPlaybookStore  # noqa: F401
from cc_headless.config.settings import S3_VECTOR_BUCKET_NAME, S3_VECTOR_PLAYBOOK_INDEX, S3_VECTOR_REGION  # noqa: F401

_bedrock = _boto3.client("bedrock-runtime", region_name=S3_VECTOR_REGION)
_embedding = BedrockEmbeddingAdapter(_bedrock)

_s3vectors = None


def _get_s3vectors_client():
    global _s3vectors  # noqa: PLW0603
    if _s3vectors is None:
        _s3vectors = _boto3.client("s3vectors", region_name=S3_VECTOR_REGION)
    return _s3vectors


def load_playbook(artifact_dir):
    store = S3VectorsPlaybookStore(_get_s3vectors_client(), _embedding)
    return store.load_playbook(artifact_dir)


def save_playbook_to_s3_vectors(playbook, rca_id, *, metric_name=""):
    store = S3VectorsPlaybookStore(_get_s3vectors_client(), _embedding)
    return store.save_to_s3_vectors(playbook, rca_id, metric_name=metric_name)
