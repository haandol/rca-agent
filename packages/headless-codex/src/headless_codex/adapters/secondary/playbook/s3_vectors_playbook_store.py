from __future__ import annotations

import json
import traceback
from pathlib import Path

import structlog

from headless_codex.config.settings import (
    DYNAMODB_TABLE_NAME,
    PLAYBOOK_TOP_K,
    S3_VECTOR_BUCKET_NAME,
    S3_VECTOR_PLAYBOOK_INDEX,
)
from headless_codex.ports.interfaces.embedding import EmbeddingPort
from headless_codex.ports.interfaces.playbook_store import PlaybookMatch, PlaybookStorePort
from headless_codex.services.playbook_merge import normalize_verification_status
from headless_codex.utils.embed_key import EMBED_FIELD_MAX, build_embed_key

logger = structlog.get_logger()


def _truncate(text: str) -> str:
    return text[:EMBED_FIELD_MAX].strip() if text else ""


def _parse_tags(raw: object) -> list[str]:
    if isinstance(raw, str):
        return [tag for tag in raw.split(",") if tag]
    if isinstance(raw, list):
        return [tag for tag in raw if isinstance(tag, str)]
    return []


class S3VectorsPlaybookStore(PlaybookStorePort):
    def __init__(self, s3_vectors_client=None, embedding: EmbeddingPort | None = None, dynamodb_client=None):
        self._s3v = s3_vectors_client
        self._embedding = embedding
        self._ddb = dynamodb_client

    @property
    def _enabled(self) -> bool:
        return bool(S3_VECTOR_BUCKET_NAME and self._s3v and self._embedding)

    def load_playbook(self, artifact_dir: Path) -> dict | None:
        path = artifact_dir / "playbook.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.error("playbook_load_failed", path=str(path), traceback=traceback.format_exc())
            return None

    def search_similar(self, query_text: str, *, threshold: float) -> list[PlaybookMatch]:
        if not self._enabled or self._embedding is None:
            return []
        if not query_text:
            return []

        try:
            query_vector = self._embedding.embed_query(query_text)
        except Exception:
            logger.error("playbook_query_embed_failed", traceback=traceback.format_exc())
            return []

        try:
            response = self._s3v.query_vectors(
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
        except Exception:
            logger.error("playbook_search_failed", traceback=traceback.format_exc())
            return []

        matches: list[PlaybookMatch] = []
        for item in response.get("vectors", []):
            # 인덱스는 거리를 돌려주므로 유사도로 뒤집는다. 이 변환을 호출자마다 다시
            # 하면 한쪽이 부호를 뒤집어 통과 조건이 반전되고, 조용히 실패한다.
            similarity = 1.0 - float(item.get("distance", 1.0))
            if similarity < threshold:
                continue
            metadata = item.get("metadata", {})
            publication_id = str(metadata.get("publication_id", ""))
            verification_status = normalize_verification_status(metadata.get("verification_status"))
            if verification_status == "VERIFIED" and not self._revision_is_published(
                metadata.get("rca_id", ""),
                item.get("key", ""),
                publication_id,
            ):
                verification_status = "DRAFT"
            matches.append(
                PlaybookMatch(
                    playbook_id=item.get("key", ""),
                    similarity=similarity,
                    failure_type=metadata.get("failure_type", ""),
                    symptom_pattern=metadata.get("symptom_pattern", ""),
                    tags=_parse_tags(metadata.get("tags")),
                    rca_id=metadata.get("rca_id", ""),
                    publication_id=publication_id,
                    verification_status=verification_status,
                )
            )
        return matches

    def _revision_is_published(
        self,
        rca_id: str,
        playbook_id: str,
        publication_id: str,
    ) -> bool:
        if not DYNAMODB_TABLE_NAME or self._ddb is None or not rca_id or not playbook_id or not publication_id:
            return False
        try:
            result = self._ddb.query(
                TableName=DYNAMODB_TABLE_NAME,
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={":pk": {"S": f"RCA#{rca_id}"}},
                ConsistentRead=True,
            )
        except Exception:
            logger.error(
                "playbook_revision_publication_check_failed",
                rca_id=rca_id,
                traceback=traceback.format_exc(),
            )
            return False
        revision = _revision_playbook(result.get("Items", []), playbook_id, publication_id)
        return revision is not None and normalize_verification_status(revision.get("verification_status")) == "VERIFIED"

    def load_detail(self, match: PlaybookMatch) -> dict | None:
        """후보의 현재 절차를 로드한다.

        회고 개정본이 분석 원본을 이긴다. 둘은 같은 파티션에 같은 플레이북 식별자로
        존재할 수 있고, 다음 실행이 근거로 삼는 것은 개정본이다 — 원본을 보강하면
        회고가 교정한 인자와 순서가 같은 식별자로 교정 이전 값으로 덮어써진다.

        어느 쪽도 읽지 못하면 None 이다. 절차를 보지 못한 상태의 "보강"은 축적을
        되돌리므로, 호출자는 이 후보를 병합 대상에서 제외해야 한다.
        """
        if not DYNAMODB_TABLE_NAME or self._ddb is None or not match.rca_id or not match.playbook_id:
            return None

        try:
            result = self._ddb.query(
                TableName=DYNAMODB_TABLE_NAME,
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={":pk": {"S": f"RCA#{match.rca_id}"}},
            )
        except Exception:
            logger.error(
                "playbook_detail_query_failed",
                rca_id=match.rca_id,
                traceback=traceback.format_exc(),
            )
            return None

        items = result.get("Items", [])

        revision = _revision_playbook(items, match.playbook_id, match.publication_id)
        if revision is not None:
            logger.info("playbook_detail_from_revision", playbook_id=match.playbook_id)
            return revision

        recorded = _span_playbook(items, match.playbook_id)
        if recorded is None:
            logger.info(
                "playbook_detail_unavailable",
                playbook_id=match.playbook_id,
                rca_id=match.rca_id,
            )
        return recorded

    def save_to_s3_vectors(
        self,
        playbook: dict,
        rca_id: str,
        *,
        metric_name: str = "",
        publication_id: str = "",
    ) -> bool:
        if not self._enabled or self._embedding is None:
            logger.info("s3_vectors_not_configured")
            return False

        playbook_id = playbook.get("playbook_id", "")
        failure_type = _truncate(str(playbook.get("failure_type", "")))
        symptom_pattern = _truncate(str(playbook.get("symptom_pattern", "")))

        embed_text = build_embed_key(
            failure_type=failure_type,
            symptom=symptom_pattern,
            metric_name=metric_name,
        )
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
            # 검증 상태는 절차 본문이 아니라 판별 값이므로 상세를 로드하지 않고도 보여야
            # 한다. 승격이 개정본에만 반영되면 검색 결과에서는 초안으로 남는다.
            "verification_status": normalize_verification_status(playbook.get("verification_status")),
        }
        if publication_id:
            metadata["publication_id"] = publication_id

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


def _revision_playbook(
    items: list[dict],
    playbook_id: str,
    publication_id: str,
) -> dict | None:
    """회고 개정본을 돌려준다. 없거나 해석 불가면 None.

    해석 불가를 병합 포기 사유로 삼지 않는다 — 아직 병합 가능한 원본이 남아 있으므로,
    호출자가 원본으로 떨어지는 편이 새 식별자로 중복 생성하는 것보다 낫다.
    """
    for item in items:
        if not item.get("SK", {}).get("S", "").endswith("#PLAYBOOK_REVISION"):
            continue
        if item.get("playbook_id", {}).get("S") != playbook_id:
            continue
        if item.get("publication_status", {}).get("S") != "PUBLISHED":
            continue
        if not publication_id or item.get("revised_by_execution_id", {}).get("S") != publication_id:
            continue
        raw = item.get("playbook", {}).get("S")
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("playbook_revision_unreadable", playbook_id=playbook_id)
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _span_playbook(items: list[dict], playbook_id: str) -> dict | None:
    """분석이 PLAYBOOK 스팬 메타데이터로 남긴 원본을 복원한다."""
    for item in items:
        if item.get("span_type", {}).get("S") != "PLAYBOOK":
            continue
        metadata = _deserialize(item.get("metadata", {}).get("M"))
        if isinstance(metadata, dict) and metadata.get("playbook_id") == playbook_id:
            return metadata
    return None


def _deserialize(raw: dict | None) -> dict | None:
    if not raw:
        return None
    return {key: _deserialize_value(value) for key, value in raw.items()}


def _deserialize_value(value: dict):
    if "S" in value:
        return value["S"]
    if "N" in value:
        number = value["N"]
        return int(number) if "." not in number else float(number)
    if "BOOL" in value:
        return value["BOOL"]
    if "M" in value:
        return {key: _deserialize_value(item) for key, item in value["M"].items()}
    if "L" in value:
        return [_deserialize_value(item) for item in value["L"]]
    return str(value)
