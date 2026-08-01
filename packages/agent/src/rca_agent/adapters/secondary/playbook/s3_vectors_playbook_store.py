from __future__ import annotations

import logging

from rca_agent.config.settings import (
    PLAYBOOK_TOP_K,
    S3_VECTOR_BUCKET_NAME,
    S3_VECTOR_PLAYBOOK_INDEX,
)
from rca_agent.ports.dto.models import (
    ExecutionStep,
    Playbook,
    PlaybookMatch,
    PlaybookVerificationStatus,
    ScopingResult,
)
from rca_agent.ports.interfaces.embedding import EmbeddingPort
from rca_agent.ports.interfaces.playbook_store import PlaybookStorePort
from rca_agent.utils.embed_key import EMBED_FIELD_MAX, build_embed_key
from rca_agent.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

_SEARCH_MAX_RETRIES = 3


def _truncate(text: str, max_len: int = EMBED_FIELD_MAX) -> str:
    return text[:max_len].strip() if text else ""


def _parse_tags(raw) -> list[str]:
    """S3 Vectors stores tags as a CSV string; older records may hold a list."""
    if isinstance(raw, str):
        return [tag for tag in raw.split(",") if tag]
    if isinstance(raw, list):
        return [tag for tag in raw if isinstance(tag, str)]
    return []


def _as_text(raw) -> str:
    return raw if isinstance(raw, str) else ""


def _as_text_list(raw) -> list[str]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    return []


def _as_verification_status(raw) -> PlaybookVerificationStatus:
    """Rebuild the recorded verification status, defaulting to a draft.

    An unreadable or absent value means the record predates the promotion or was
    written by something we cannot trust, and an unproven procedure must not read
    as verified. Only the recorded VERIFIED survives a reload.
    """
    if isinstance(raw, str):
        try:
            return PlaybookVerificationStatus(raw)
        except ValueError:
            logger.info("Unknown playbook verification status %r, treating as draft", raw)
    return PlaybookVerificationStatus.DRAFT


def _as_execution_steps(raw) -> list[ExecutionStep]:
    """Rebuild the recorded execution steps so a merge starts from what ran.

    A step whose recorded shape is unreadable is dropped rather than guessed at:
    the retrospective keys corrections by ``step_id``, so a step with no
    identifier cannot be corrected and must not become an execution basis.
    """
    if not isinstance(raw, list):
        return []
    steps: list[ExecutionStep] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        step_id = _as_text(entry.get("step_id")).strip()
        if not step_id:
            continue
        steps.append(
            ExecutionStep(
                step_id=step_id,
                intent=_as_text(entry.get("intent")),
                action=_as_text(entry.get("action")),
                success_criteria=_as_text(entry.get("success_criteria")),
            )
        )
    return steps


class S3VectorsPlaybookStore(PlaybookStorePort):
    def __init__(
        self,
        s3_vectors_client=None,
        embedding: EmbeddingPort | None = None,
        dynamodb_client=None,
    ):
        self._s3v = s3_vectors_client
        self._embedding = embedding
        self._dynamodb = dynamodb_client

    @property
    def _enabled(self) -> bool:
        return bool(S3_VECTOR_BUCKET_NAME and self._s3v)

    def search_similar(self, query_text: str, *, threshold: float) -> list[PlaybookMatch]:
        if not self._enabled or self._embedding is None:
            return []
        try:
            query_vector = self._embedding.embed_query(query_text)
        except Exception:
            logger.exception("Failed to embed query text, skipping playbook search")
            return []

        def query() -> dict:
            return self._s3v.query_vectors(
                vectorBucketName=S3_VECTOR_BUCKET_NAME,
                indexName=S3_VECTOR_PLAYBOOK_INDEX,
                queryVector={"float32": query_vector},
                topK=PLAYBOOK_TOP_K,
                returnMetadata=True,
                # 거리를 요청하지 않으면 응답에 그 필드가 없다. 없는 값을 최대 거리로
                # 읽으면 모든 후보의 유사도가 0이 되어 임계값에서 전부 탈락하고,
                # 검색은 오류 없이 빈 결과만 돌려준다.
                returnDistance=True,
            )

        response = retry_with_backoff(
            query,
            max_retries=_SEARCH_MAX_RETRIES,
            operation="playbook search",
        )
        if response is None:
            return []

        matches = []
        for item in response.get("vectors", []):
            similarity = 1.0 - float(item.get("distance", 1.0))
            if similarity < threshold:
                continue
            metadata = item.get("metadata", {})
            matches.append(
                PlaybookMatch(
                    playbook_id=item.get("key", ""),
                    similarity=similarity,
                    failure_type=metadata.get("failure_type", ""),
                    symptom_pattern=metadata.get("symptom_pattern", ""),
                    tags=_parse_tags(metadata.get("tags")),
                    rca_id=metadata.get("rca_id", ""),
                    verification_status=_as_verification_status(metadata.get("verification_status")),
                )
            )
        return matches

    def load_detail(self, match: PlaybookMatch) -> Playbook | None:
        from rca_agent.adapters.secondary.trace.dynamodb_trace_store import TraceStore

        recorded = TraceStore.get_playbook_metadata(
            match.rca_id,
            match.playbook_id,
            dynamodb_client=self._dynamodb,
        )
        if not recorded:
            logger.info(
                "Playbook %s detail unavailable (rca_id=%s)",
                match.playbook_id,
                match.rca_id or "unknown",
            )
            return None

        return Playbook(
            playbook_id=match.playbook_id,
            failure_type=_as_text(recorded.get("failure_type")) or match.failure_type,
            symptom_pattern=_as_text(recorded.get("symptom_pattern")) or match.symptom_pattern,
            severity_criteria=_as_text(recorded.get("severity_criteria")),
            verification_steps=_as_text_list(recorded.get("verification_steps")),
            execution_steps=_as_execution_steps(recorded.get("execution_steps")),
            temporary_mitigation=_as_text(recorded.get("temporary_mitigation")),
            permanent_remediation=_as_text(recorded.get("permanent_remediation")),
            escalation_criteria=_as_text(recorded.get("escalation_criteria")),
            prevention_measures=_as_text_list(recorded.get("prevention_measures")),
            related_metrics=_as_text_list(recorded.get("related_metrics")),
            rca_id=match.rca_id,
            tags=_as_text_list(recorded.get("tags")) or match.tags,
            # 검증 상태를 재구성하지 않으면 기본값 DRAFT로 되살아나, 보강 경로가 회고의
            # 승격을 조용히 취소한다.
            verification_status=_as_verification_status(recorded.get("verification_status")),
        )

    def save(self, playbook: Playbook, *, scoping_result: ScopingResult | None = None) -> bool:
        if not self._enabled or self._embedding is None:
            logger.info("S3 Vectors not configured, skipping playbook indexing")
            return False

        metric_name = ""
        if scoping_result and scoping_result.raw_alarm and scoping_result.raw_alarm.trigger:
            metric_name = scoping_result.raw_alarm.trigger.metric_name

        embed_text = build_embed_key(
            failure_type=playbook.failure_type,
            symptom=playbook.symptom_pattern,
            metric_name=metric_name,
        )
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
            # Whether a procedure was proven by an execution is a discriminator, not
            # detail: a searcher has to see it without loading the record behind the
            # hit, and the promotion has to be visible on both the revision and the
            # index or it only shows on one path.
            "verification_status": playbook.verification_status.value,
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
