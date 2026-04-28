from __future__ import annotations

import logging
import time

from rca_agent.config.settings import (
    PLAYBOOK_SIMILARITY_THRESHOLD,
    PLAYBOOK_TOP_K,
    S3_VECTOR_BUCKET_NAME,
    S3_VECTOR_PLAYBOOK_INDEX,
)
from rca_agent.ports.dto.models import Playbook, PlaybookMatch, ScopingResult
from rca_agent.ports.interfaces.embedding import EmbeddingPort
from rca_agent.ports.interfaces.playbook_store import PlaybookStorePort

logger = logging.getLogger(__name__)

_SEARCH_MAX_RETRIES = 3
_SEARCH_BASE_DELAY = 1.0
_EMBED_FIELD_MAX = 80


def _truncate(text: str, max_len: int = _EMBED_FIELD_MAX) -> str:
    return text[:max_len].strip() if text else ""


class S3VectorsPlaybookStore(PlaybookStorePort):
    def __init__(self, s3_vectors_client=None, embedding: EmbeddingPort | None = None):
        self._s3v = s3_vectors_client
        self._embedding = embedding

    @property
    def _enabled(self) -> bool:
        return bool(S3_VECTOR_BUCKET_NAME and self._s3v)

    def search_similar(self, query_text: str) -> list[PlaybookMatch]:
        if not self._enabled or self._embedding is None:
            return []
        try:
            query_vector = self._embedding.embed_query(query_text)
        except Exception:
            logger.exception("Failed to embed query text, skipping playbook search")
            return []

        for attempt in range(_SEARCH_MAX_RETRIES):
            try:
                response = self._s3v.query_vectors(
                    vectorBucketName=S3_VECTOR_BUCKET_NAME,
                    indexName=S3_VECTOR_PLAYBOOK_INDEX,
                    queryVector={"float32": query_vector},
                    topK=PLAYBOOK_TOP_K,
                )
                break
            except Exception:
                if attempt == _SEARCH_MAX_RETRIES - 1:
                    logger.exception("Failed to search playbooks after %d attempts", _SEARCH_MAX_RETRIES)
                    return []
                delay = _SEARCH_BASE_DELAY * (2**attempt)
                logger.warning("Playbook search attempt %d failed, retrying in %.1fs", attempt + 1, delay)
                time.sleep(delay)

        matches = []
        for item in response.get("vectors", []):
            similarity = item.get("distance", 0.0)
            if similarity < PLAYBOOK_SIMILARITY_THRESHOLD:
                continue
            metadata = item.get("metadata", {})
            matches.append(
                PlaybookMatch(
                    playbook_id=item.get("key", ""),
                    title=metadata.get("failure_type", "Unknown"),
                    similarity=similarity,
                    root_cause_summary=metadata.get("symptom_pattern", ""),
                )
            )
        return matches

    def save(self, playbook: Playbook, *, scoping_result: ScopingResult | None = None) -> bool:
        if not self._enabled or self._embedding is None:
            logger.info("S3 Vectors not configured, skipping playbook indexing")
            return False

        metric_name = ""
        if scoping_result and scoping_result.raw_alarm and scoping_result.raw_alarm.trigger:
            metric_name = scoping_result.raw_alarm.trigger.metric_name

        parts = {
            "장애유형": _truncate(playbook.failure_type),
            "증상": _truncate(playbook.symptom_pattern),
            "메트릭": _truncate(metric_name),
        }
        embed_text = " | ".join(f"{k}: {v}" for k, v in parts.items() if v)
        try:
            vector = self._embedding.embed_document(embed_text)
        except Exception:
            logger.exception("Failed to embed playbook text")
            return False

        metadata = {
            "failure_type": _truncate(playbook.failure_type),
            "symptom_pattern": _truncate(playbook.symptom_pattern),
            "tags": ",".join(playbook.tags)[:256],
            "rca_id": playbook.rca_id,
        }
        try:
            self._s3v.put_vectors(
                vectorBucketName=S3_VECTOR_BUCKET_NAME,
                indexName=S3_VECTOR_PLAYBOOK_INDEX,
                vectors=[
                    {
                        "key": playbook.playbook_id,
                        "data": {"float32": vector},
                        "metadata": metadata,
                    }
                ],
            )
            logger.info("Playbook %s indexed in S3 Vectors", playbook.playbook_id)
            return True
        except Exception:
            logger.exception("Failed to index playbook in S3 Vectors")
            return False
