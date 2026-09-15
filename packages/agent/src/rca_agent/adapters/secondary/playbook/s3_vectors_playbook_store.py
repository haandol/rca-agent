from __future__ import annotations

import logging
import math

from rca_agent.adapters.secondary.playbook.library import PlaybookLibrary, matched_comparison
from rca_agent.config.settings import (
    DYNAMODB_TABLE_NAME,
    ENGINE,
    PLAYBOOK_TOP_K,
    S3_VECTOR_BUCKET_NAME,
    S3_VECTOR_PLAYBOOK_INDEX,
    SESSION_TTL_DAYS,
)
from rca_agent.ports.dto.models import Playbook, PlaybookMatch, ScopingResult
from rca_agent.ports.interfaces.embedding import EmbeddingPort
from rca_agent.ports.interfaces.playbook_store import (
    PlaybookArchiveUnavailable,
    PlaybookSearchUnavailable,
    PlaybookStorePort,
)
from rca_agent.utils.embed_key import EMBED_FIELD_MAX, build_embed_key

logger = logging.getLogger(__name__)


class S3VectorsPlaybookStore(PlaybookStorePort):
    def __init__(self, s3_vectors_client=None, embedding: EmbeddingPort | None = None, dynamodb_client=None):
        """Keep vector pointers and authoritative state clients together for fail-closed reads."""
        self._s3v, self._embedding, self._ddb = s3_vectors_client, embedding, dynamodb_client

    @property
    def _library(self) -> PlaybookLibrary:
        """Resolve configured table at use time, sharing the same wire with either engine."""
        return PlaybookLibrary(self._ddb, DYNAMODB_TABLE_NAME, SESSION_TTL_DAYS)

    @property
    def _enabled(self) -> bool:
        """Search and publication need both the vector index and its source authority."""
        return bool(S3_VECTOR_BUCKET_NAME and self._s3v and self._embedding and self._ddb and DYNAMODB_TABLE_NAME)

    def search_similar(self, query_text: str, *, threshold: float) -> list[PlaybookMatch]:
        """Retain relevant unavailable identities for diagnostics without exposing their historical bodies."""
        if not self._enabled or not query_text:
            raise PlaybookSearchUnavailable("playbook search is not configured or query is empty")
        try:
            vector = self._embedding.embed_query(query_text)
            response = self._s3v.query_vectors(
                vectorBucketName=S3_VECTOR_BUCKET_NAME,
                indexName=S3_VECTOR_PLAYBOOK_INDEX,
                queryVector={"float32": vector},
                topK=PLAYBOOK_TOP_K,
                returnMetadata=True,
                returnDistance=True,
            )
            rows = response.get("vectors", [])
            if not isinstance(rows, list):
                raise ValueError("vector query returned invalid candidates")
        except Exception as exc:
            raise PlaybookSearchUnavailable("playbook embedding or vector query failed") from exc
        matches = []
        for item in rows:
            if not isinstance(item, dict):
                logger.warning("Skipping malformed playbook vector result")
                continue
            distance = item.get("distance")
            if isinstance(distance, bool) or not isinstance(distance, (int, float)) or not math.isfinite(distance):
                logger.warning("Skipping playbook vector with invalid distance: %s", item.get("key"))
                continue
            similarity = 1.0 - distance
            if similarity < threshold:
                continue
            metadata = item.get("metadata", {})
            if not isinstance(metadata, dict):
                logger.warning("Skipping playbook vector with invalid metadata: %s", item.get("key"))
                continue
            playbook_id = metadata.get("playbook_id") or item.get("key", "")
            if not isinstance(playbook_id, str) or not playbook_id:
                logger.warning("Skipping playbook vector with invalid identity")
                continue
            identity = {
                name: metadata.get(name, "") if isinstance(metadata.get(name, ""), str) else ""
                for name in ("rca_id", "engine", "library_revision", "publication_id")
            }
            revision = identity["library_revision"]
            unavailable = ""
            detail = None
            if revision and revision != "legacy" and item.get("key") != f"{playbook_id}@{revision}":
                unavailable = "vector identity mismatch"
            else:
                try:
                    detail = self._library.load(
                        playbook_id, identity["rca_id"], identity["engine"], revision, identity["publication_id"]
                    )
                except Exception as exc:
                    unavailable = f"published detail lookup failed ({type(exc).__name__})"
            if detail is None:
                matches.append(
                    PlaybookMatch(
                        playbook_id=playbook_id,
                        similarity=similarity,
                        **identity,
                        unavailable_reason=unavailable or "published detail unavailable",
                    )
                )
                continue
            tags = detail.get("tags", [])
            matches.append(
                PlaybookMatch(
                    playbook_id=playbook_id,
                    similarity=similarity,
                    failure_type=detail.get("failure_type", ""),
                    symptom_pattern=detail.get("symptom_pattern", ""),
                    tags=tags.split(",") if isinstance(tags, str) else tags,
                    rca_id=detail["source_rca_id"],
                    engine=detail["source_engine"],
                    library_revision=detail["library_revision"],
                    publication_id=identity["publication_id"],
                    verification_status=detail.get("verification_status", "DRAFT"),
                )
            )
        return matches

    def load_detail(self, match: PlaybookMatch) -> Playbook | None:
        """Recheck the head on detail reads so a candidate cannot survive a concurrent update."""
        if match.unavailable_reason == "vector identity mismatch":
            return None
        try:
            detail = self._library.load(
                match.playbook_id, match.rca_id, match.engine, match.library_revision, match.publication_id
            )
            return Playbook.model_validate(detail) if detail is not None else None
        except Exception:
            logger.exception("Playbook detail unavailable: %s", match.playbook_id)
            return None

    def _publish(
        self,
        playbook: dict,
        rca_id: str,
        *,
        metric_name: str = "",
        publication_id: str = "",
        baseline_playbook: dict | None = None,
        source_engine: str = "",
        publication_result: dict | None = None,
    ) -> bool:
        """Stage immutable content before vector writes; matched proposals never replace public knowledge."""
        if not publication_id and matched_comparison(playbook):
            return True
        if not self._enabled:
            return False
        revision = f"retrospective:{publication_id}" if publication_id else f"analysis:{rca_id}"
        try:
            if publication_id and baseline_playbook is None:
                raise ValueError("retrospective requires its exact baseline")
            if publication_id:
                baseline_playbook = self._library.retrospective_baseline(baseline_playbook, rca_id)
            head = self._library.stage(
                playbook,
                rca_id,
                revision,
                metric_name=metric_name,
                engine=source_engine or ENGINE,
                baseline=baseline_playbook,
                publication_result=publication_result,
            )
            if head["publication_status"] == "PUBLISHED":
                return True
            failure_type = str(playbook.get("failure_type", ""))[:EMBED_FIELD_MAX].strip()
            symptom = str(playbook.get("symptom_pattern", ""))[:EMBED_FIELD_MAX].strip()
            vector = self._embedding.embed_document(
                build_embed_key(
                    failure_type=failure_type,
                    symptom=symptom,
                    metric_name=metric_name,
                )
            )
            metadata = {
                "playbook_id": playbook["playbook_id"],
                "library_revision": revision,
                "failure_type": failure_type,
                "symptom_pattern": symptom,
                "tags": ",".join(playbook.get("tags", []))[:256],
                "rca_id": rca_id,
                "engine": head["engine"],
                "verification_status": playbook.get("verification_status", "DRAFT"),
            }
            if publication_id:
                metadata["publication_id"] = publication_id
            self._s3v.put_vectors(
                vectorBucketName=S3_VECTOR_BUCKET_NAME,
                indexName=S3_VECTOR_PLAYBOOK_INDEX,
                vectors=[{"key": head["vector_key"], "data": {"float32": vector}, "metadata": metadata}],
            )
            # A retrospective's original revision must commit before library visibility.
            if not publication_id:
                self._finalize(head)
            return True
        except Exception:
            logger.exception("Playbook publication failed: %s", playbook.get("playbook_id"))
            return False

    def _finalize(self, head: dict) -> None:
        """Fence publication by revision, then clean up only the previous immutable vector key."""
        self._library.finalize(head)
        previous = head.get("previous_vector_key")
        if previous and previous != head["vector_key"]:
            try:
                self._s3v.delete_vectors(
                    vectorBucketName=S3_VECTOR_BUCKET_NAME, indexName=S3_VECTOR_PLAYBOOK_INDEX, keys=[previous]
                )
            except Exception:
                logger.exception("Old playbook vector cleanup failed; stale key remains excluded")

    def save(self, playbook: Playbook, *, scoping_result: ScopingResult | None = None, metric_name: str = "") -> bool:
        """Publish the full completed DTO, retaining comparison evidence in every snapshot."""
        if not metric_name and scoping_result and scoping_result.raw_alarm and scoping_result.raw_alarm.trigger:
            metric_name = scoping_result.raw_alarm.trigger.metric_name
        return self._publish(playbook.model_dump(mode="json"), playbook.rca_id, metric_name=metric_name)

    def archive_comparison(self, playbook: Playbook, rca_id: str, engine: str) -> Playbook:
        """Keep full comparison inputs in a sixty-day original before tracing or completing the incident."""
        try:
            archived = self._library.archive_comparison(playbook.model_dump(mode="json"), rca_id, engine)
            return playbook.model_copy(deep=True, update={"comparison": archived.get("comparison", {})})
        except Exception as exc:
            raise PlaybookArchiveUnavailable("comparison original could not be archived") from exc
