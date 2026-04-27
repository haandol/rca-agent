"""Save playbook to S3 Vectors for future similar-playbook retrieval."""

from __future__ import annotations

import json
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
        logger.exception("playbook_load_failed", path=str(path))
        return None


def save_playbook_to_s3_vectors(playbook: dict, rca_id: str, *, metric_name: str = "") -> bool:
    if not S3_VECTOR_BUCKET_NAME:
        logger.info("s3_vectors_not_configured")
        return False

    playbook_id = playbook.get("playbook_id", "")
    failure_type = playbook.get("failure_type", "")
    symptom_pattern = playbook.get("symptom_pattern", "")

    embed_text = " | ".join(p for p in [failure_type, metric_name, symptom_pattern] if p)
    if not embed_text:
        logger.warning("playbook_empty_embed_text", rca_id=rca_id)
        return False

    vector = embed_document(embed_text)

    metadata = {
        "failure_type": failure_type,
        "symptom_pattern": symptom_pattern,
        "verification_steps": playbook.get("verification_steps", []),
        "temporary_mitigation": playbook.get("temporary_mitigation", ""),
        "permanent_remediation": playbook.get("permanent_remediation", ""),
        "prevention_measures": playbook.get("prevention_measures", []),
        "tags": playbook.get("tags", []),
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
        logger.exception("playbook_index_failed", playbook_id=playbook_id, rca_id=rca_id)
        return False
