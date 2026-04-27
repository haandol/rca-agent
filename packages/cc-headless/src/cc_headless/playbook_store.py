"""Save playbook to S3 Vectors for future similar-playbook retrieval."""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import boto3
import structlog

from cc_headless.config import S3_VECTOR_BUCKET_NAME, S3_VECTOR_PLAYBOOK_INDEX, S3_VECTOR_REGION
from cc_headless.embeddings import embed_document

logger = structlog.get_logger()

_s3vectors = None


def _get_s3vectors_client():
    global _s3vectors  # noqa: PLW0603
    if _s3vectors is None:
        _s3vectors = boto3.client("s3vectors", region_name=S3_VECTOR_REGION)
    return _s3vectors


def load_playbook(artifact_dir: Path) -> dict | None:
    path = artifact_dir / "playbook.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        logger.error("playbook_load_failed", path=str(path), traceback=traceback.format_exc())
        return None


def save_playbook_to_s3_vectors(playbook: dict, rca_id: str, *, metric_name: str = "") -> bool:
    if not S3_VECTOR_BUCKET_NAME:
        logger.info("s3_vectors_not_configured")
        return False

    max_len = 80
    playbook_id = playbook.get("playbook_id", "")
    failure_type = playbook.get("failure_type", "")[:max_len]
    symptom_pattern = playbook.get("symptom_pattern", "")[:max_len]

    parts = {
        "장애유형": failure_type,
        "증상": symptom_pattern,
        "메트릭": metric_name[:max_len],
    }
    embed_text = " | ".join(f"{k}: {v}" for k, v in parts.items() if v)
    if not embed_text:
        logger.warning("playbook_empty_embed_text", rca_id=rca_id)
        return False

    try:
        vector = embed_document(embed_text)
    except Exception:
        logger.error("playbook_embed_failed", rca_id=rca_id, traceback=traceback.format_exc())
        return False

    metadata = {
        "failure_type": failure_type,
        "symptom_pattern": symptom_pattern,
        "tags": ",".join(playbook.get("tags", []))[:256],
        "rca_id": rca_id,
    }

    try:
        client = _get_s3vectors_client()
        client.put_vectors(
            vectorBucketName=S3_VECTOR_BUCKET_NAME,
            indexName=S3_VECTOR_PLAYBOOK_INDEX,
            vectors=[
                {
                    "key": playbook_id,
                    "data": {"float32": vector},
                    "metadata": metadata,
                }
            ],
        )
        logger.info("playbook_indexed", playbook_id=playbook_id, rca_id=rca_id)
        return True
    except Exception:
        logger.error("playbook_index_failed", playbook_id=playbook_id, rca_id=rca_id, traceback=traceback.format_exc())
        return False
