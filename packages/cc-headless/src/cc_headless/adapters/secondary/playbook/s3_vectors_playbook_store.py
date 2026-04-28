from __future__ import annotations

import json
import traceback
from pathlib import Path

import structlog

from cc_headless.config.settings import S3_VECTOR_BUCKET_NAME, S3_VECTOR_PLAYBOOK_INDEX
from cc_headless.ports.interfaces.embedding import EmbeddingPort
from cc_headless.ports.interfaces.playbook_store import PlaybookStorePort

logger = structlog.get_logger()


class S3VectorsPlaybookStore(PlaybookStorePort):
    def __init__(self, s3_vectors_client=None, embedding: EmbeddingPort | None = None):
        self._s3v = s3_vectors_client
        self._embedding = embedding

    def load_playbook(self, artifact_dir: Path) -> dict | None:
        path = artifact_dir / "playbook.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.error("playbook_load_failed", path=str(path), traceback=traceback.format_exc())
            return None

    def save_to_s3_vectors(self, playbook: dict, rca_id: str, *, metric_name: str = "") -> bool:
        if not S3_VECTOR_BUCKET_NAME or not self._s3v or not self._embedding:
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
            vector = self._embedding.embed_document(embed_text)
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
            self._s3v.put_vectors(
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
            logger.error(
                "playbook_index_failed",
                playbook_id=playbook_id,
                rca_id=rca_id,
                traceback=traceback.format_exc(),
            )
            return False
