"""플레이북 인덱스 어댑터의 계약.

검색은 되지만 상세가 낡은 문서를 가리키면 병합이 정상 동작하면서 결과가 퇴행한다.
실패로 드러나지 않으므로 이 우선순위를 테스트가 고정한다.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from cc_headless.adapters.secondary.playbook.s3_vectors_playbook_store import S3VectorsPlaybookStore
from cc_headless.config.settings import PLAYBOOK_UPDATE_THRESHOLD
from cc_headless.ports.interfaces.playbook_store import PlaybookMatch

_MODULE = "cc_headless.adapters.secondary.playbook.s3_vectors_playbook_store"


def _match(
    playbook_id: str = "pb-1",
    rca_id: str = "rca-7",
    publication_id: str = "exec-9",
) -> PlaybookMatch:
    return PlaybookMatch(
        playbook_id=playbook_id,
        similarity=0.9,
        rca_id=rca_id,
        publication_id=publication_id,
    )


def _span_item() -> dict:
    return {
        "SK": {"S": "cc-headless#SPAN#s-1"},
        "span_type": {"S": "PLAYBOOK"},
        "metadata": {
            "M": {
                "playbook_id": {"S": "pb-1"},
                "failure_type": {"S": "DB connection leak"},
                "temporary_mitigation": {"S": "원본 조치"},
                "verification_status": {"S": "DRAFT"},
                "execution_steps": {
                    "L": [
                        {
                            "M": {
                                "step_id": {"S": "step-1"},
                                "action": {"S": "잘못된 인자로 갱신한다"},
                                "success_criteria": {"S": "커넥션 감소"},
                            }
                        }
                    ]
                },
            }
        },
    }


def _revision_item(playbook_id: str = "pb-1") -> dict:
    revised = {
        "playbook_id": playbook_id,
        "failure_type": "DB connection leak",
        "temporary_mitigation": "교정된 조치",
        "verification_status": "VERIFIED",
        "execution_steps": [
            {
                "step_id": "step-1",
                "action": "교정된 인자로 갱신한다",
                "success_criteria": "커넥션 감소",
            },
            {
                "step_id": "step-2",
                "action": "커넥션 메트릭을 조회한다",
                "success_criteria": "임계치 미만 유지",
            },
        ],
    }
    return {
        "SK": {"S": "cc-headless#PLAYBOOK_REVISION"},
        "playbook_id": {"S": playbook_id},
        "playbook": {"S": json.dumps(revised, ensure_ascii=False)},
        "revised_by_execution_id": {"S": "exec-9"},
        "publication_status": {"S": "PUBLISHED"},
    }


def _staged_revision_item(playbook_id: str = "pb-1") -> dict:
    item = _revision_item(playbook_id)
    item["SK"] = {"S": "cc-headless#PLAYBOOK_REVISION_STAGE#exec-9"}
    item["publication_status"] = {"S": "PENDING"}
    return item


class TestLoadDetailPrefersTheRetrospectiveRevision:
    def _store(self, ddb) -> S3VectorsPlaybookStore:
        return S3VectorsPlaybookStore(dynamodb_client=ddb)

    @patch(f"{_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_revision_wins_over_the_analysis_span(self):
        ddb = MagicMock()
        ddb.query.return_value = {"Items": [_span_item(), _revision_item()]}

        detail = self._store(ddb).load_detail(_match())

        assert detail is not None
        assert detail["temporary_mitigation"] == "교정된 조치"
        assert [step["step_id"] for step in detail["execution_steps"]] == ["step-1", "step-2"]
        assert detail["execution_steps"][0]["action"] == "교정된 인자로 갱신한다"

    @patch(f"{_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_promotion_survives_the_reload(self):
        ddb = MagicMock()
        ddb.query.return_value = {"Items": [_span_item(), _revision_item()]}

        detail = self._store(ddb).load_detail(_match())

        assert detail is not None
        assert detail["verification_status"] == "VERIFIED"

    @patch(f"{_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_pending_revision_is_not_loaded_after_a_failed_commit(self):
        ddb = MagicMock()
        ddb.query.return_value = {"Items": [_span_item(), _staged_revision_item()]}

        detail = self._store(ddb).load_detail(_match())

        assert detail is not None
        assert detail["verification_status"] == "DRAFT"
        assert detail["temporary_mitigation"] == "원본 조치"

    @patch(f"{_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_falls_back_to_the_span_without_a_revision(self):
        ddb = MagicMock()
        ddb.query.return_value = {"Items": [_span_item()]}

        detail = self._store(ddb).load_detail(_match())

        assert detail is not None
        assert detail["temporary_mitigation"] == "원본 조치"

    @patch(f"{_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_unreadable_revision_falls_back_rather_than_dropping_the_playbook(self):
        ddb = MagicMock()
        broken = _revision_item()
        broken["playbook"] = {"S": "{not json"}
        ddb.query.return_value = {"Items": [_span_item(), broken]}

        detail = self._store(ddb).load_detail(_match())

        assert detail is not None
        assert detail["temporary_mitigation"] == "원본 조치"

    @patch(f"{_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_ignores_a_revision_of_a_different_playbook(self):
        ddb = MagicMock()
        ddb.query.return_value = {"Items": [_span_item(), _revision_item("pb-other")]}

        detail = self._store(ddb).load_detail(_match())

        assert detail is not None
        assert detail["temporary_mitigation"] == "원본 조치"

    @patch(f"{_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_returns_none_when_neither_record_is_readable(self):
        ddb = MagicMock()
        ddb.query.return_value = {"Items": []}

        # 절차를 보지 못한 상태의 보강은 축적을 되돌리므로 호출자가 후보를 건너뛰어야 한다.
        assert self._store(ddb).load_detail(_match()) is None

    @patch(f"{_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_query_failure_is_reported_as_unavailable_not_empty(self):
        ddb = MagicMock()
        ddb.query.side_effect = RuntimeError("throttled")

        assert self._store(ddb).load_detail(_match()) is None

    def test_returns_none_without_a_dynamodb_client(self):
        assert self._store(None).load_detail(_match()) is None

    @patch(f"{_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_returns_none_when_the_hit_has_no_rca_id(self):
        ddb = MagicMock()

        assert self._store(ddb).load_detail(_match(rca_id="")) is None
        ddb.query.assert_not_called()


class TestSearchSimilar:
    def _store(self, s3v, embedding) -> S3VectorsPlaybookStore:
        return S3VectorsPlaybookStore(s3v, embedding)

    def _embedding(self) -> MagicMock:
        embedding = MagicMock()
        embedding.embed_query.return_value = [0.1, 0.2]
        return embedding

    @patch(f"{_MODULE}.S3_VECTOR_BUCKET_NAME", "vectors")
    def test_converts_distance_to_similarity_and_applies_the_threshold(self):
        s3v = MagicMock()
        s3v.query_vectors.return_value = {
            "vectors": [
                {"key": "pb-near", "distance": 0.05, "metadata": {"rca_id": "rca-1"}},
                {"key": "pb-far", "distance": 0.5, "metadata": {"rca_id": "rca-2"}},
            ]
        }

        matches = self._store(s3v, self._embedding()).search_similar("query", threshold=PLAYBOOK_UPDATE_THRESHOLD)

        assert [match.playbook_id for match in matches] == ["pb-near"]

    @patch(f"{_MODULE}.S3_VECTOR_BUCKET_NAME", "vectors")
    def test_query_explicitly_requests_distance(self):
        """거리를 요청하지 않으면 응답에 그 필드가 없어 모든 후보가 탈락한다.

        유사도는 거리에서만 나온다. 없는 거리를 최대값으로 읽으면 유사도가 0이 되어
        임계값이 무엇이든 통과하는 후보가 없고, 검색은 오류 없이 빈 결과만 돌려준다.
        """
        s3v = MagicMock()
        s3v.query_vectors.return_value = {"vectors": []}

        self._store(s3v, self._embedding()).search_similar("query", threshold=PLAYBOOK_UPDATE_THRESHOLD)

        assert s3v.query_vectors.call_args.kwargs["returnDistance"] is True

    @patch(f"{_MODULE}.S3_VECTOR_BUCKET_NAME", "vectors")
    def test_a_hit_without_a_distance_is_dropped(self):
        s3v = MagicMock()
        s3v.query_vectors.return_value = {"vectors": [{"key": "pb-1", "metadata": {"rca_id": "rca-1"}}]}

        matches = self._store(s3v, self._embedding()).search_similar("query", threshold=PLAYBOOK_UPDATE_THRESHOLD)

        # 유사도를 추측해 순위에 넣지 않는다 — 무관한 플레이북에 병합될 수 있다.
        assert matches == []

    @patch(f"{_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    @patch(f"{_MODULE}.S3_VECTOR_BUCKET_NAME", "vectors")
    def test_hit_reports_whether_the_procedure_was_proven(self):
        s3v = MagicMock()
        s3v.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "pb-1",
                    "distance": 0.01,
                    "metadata": {
                        "rca_id": "rca-1",
                        "verification_status": "VERIFIED",
                        "publication_id": "exec-9",
                    },
                }
            ]
        }
        ddb = MagicMock()
        ddb.query.return_value = {"Items": [_revision_item()]}

        store = S3VectorsPlaybookStore(s3v, self._embedding(), ddb)
        matches = store.search_similar("query", threshold=PLAYBOOK_UPDATE_THRESHOLD)

        assert matches[0].verification_status == "VERIFIED"

    @patch(f"{_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    @patch(f"{_MODULE}.S3_VECTOR_BUCKET_NAME", "vectors")
    def test_verified_vector_is_downgraded_when_revision_commit_is_missing(self):
        s3v = MagicMock()
        s3v.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "pb-1",
                    "distance": 0.01,
                    "metadata": {
                        "rca_id": "rca-1",
                        "verification_status": "VERIFIED",
                        "publication_id": "exec-9",
                    },
                }
            ]
        }
        ddb = MagicMock()
        ddb.query.return_value = {"Items": [_staged_revision_item()]}

        store = S3VectorsPlaybookStore(s3v, self._embedding(), ddb)
        matches = store.search_similar("query", threshold=PLAYBOOK_UPDATE_THRESHOLD)

        assert matches[0].verification_status == "DRAFT"

    @patch(f"{_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    @patch(f"{_MODULE}.S3_VECTOR_BUCKET_NAME", "vectors")
    def test_verified_vector_is_downgraded_when_committed_revision_is_draft(self):
        s3v = MagicMock()
        s3v.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "pb-1",
                    "distance": 0.01,
                    "metadata": {
                        "rca_id": "rca-1",
                        "verification_status": "VERIFIED",
                        "publication_id": "exec-9",
                    },
                }
            ]
        }
        revision = _revision_item()
        playbook = json.loads(revision["playbook"]["S"])
        playbook["verification_status"] = "DRAFT"
        revision["playbook"] = {"S": json.dumps(playbook)}
        ddb = MagicMock()
        ddb.query.return_value = {"Items": [revision]}

        store = S3VectorsPlaybookStore(s3v, self._embedding(), ddb)
        matches = store.search_similar("query", threshold=PLAYBOOK_UPDATE_THRESHOLD)

        assert matches[0].verification_status == "DRAFT"

    @patch(f"{_MODULE}.S3_VECTOR_BUCKET_NAME", "vectors")
    def test_a_record_without_a_status_reads_as_a_draft(self):
        s3v = MagicMock()
        s3v.query_vectors.return_value = {
            "vectors": [{"key": "pb-1", "distance": 0.01, "metadata": {"rca_id": "rca-1"}}]
        }

        matches = self._store(s3v, self._embedding()).search_similar("query", threshold=PLAYBOOK_UPDATE_THRESHOLD)

        # 미검증 절차가 검증됨으로 보이면 사람이 승인 판단을 잘못한다.
        assert matches[0].verification_status == "DRAFT"

    @patch(f"{_MODULE}.S3_VECTOR_BUCKET_NAME", "vectors")
    def test_uses_the_query_input_type_not_the_document_one(self):
        s3v = MagicMock()
        s3v.query_vectors.return_value = {"vectors": []}
        embedding = self._embedding()

        self._store(s3v, embedding).search_similar("query", threshold=PLAYBOOK_UPDATE_THRESHOLD)

        # 저장과 검색이 같은 입력 유형을 쓰면 같은 장애의 두 벡터가 어긋난다.
        embedding.embed_query.assert_called_once_with("query")
        embedding.embed_document.assert_not_called()

    @patch(f"{_MODULE}.S3_VECTOR_BUCKET_NAME", "vectors")
    def test_search_failure_returns_no_hits_rather_than_raising(self):
        s3v = MagicMock()
        s3v.query_vectors.side_effect = RuntimeError("index down")

        assert self._store(s3v, self._embedding()).search_similar("query", threshold=PLAYBOOK_UPDATE_THRESHOLD) == []

    def test_returns_no_hits_when_the_index_is_not_configured(self):
        assert self._store(None, self._embedding()).search_similar("query", threshold=PLAYBOOK_UPDATE_THRESHOLD) == []


class TestIndexMetadata:
    @patch(f"{_MODULE}.S3_VECTOR_BUCKET_NAME", "vectors")
    def test_indexes_the_verification_status_alongside_the_playbook(self):
        s3v = MagicMock()
        embedding = MagicMock()
        embedding.embed_document.return_value = [0.1]
        store = S3VectorsPlaybookStore(s3v, embedding)

        store.save_to_s3_vectors(
            {
                "playbook_id": "pb-1",
                "failure_type": "DB connection leak",
                "symptom_pattern": "커넥션 상승",
                "verification_status": "VERIFIED",
            },
            "rca-1",
            metric_name="DatabaseConnections",
        )

        metadata = s3v.put_vectors.call_args.kwargs["vectors"][0]["metadata"]
        # 승격이 개정본에만 반영되면 검색 결과에서는 초안으로 남는다.
        assert metadata["verification_status"] == "VERIFIED"

    @patch(f"{_MODULE}.S3_VECTOR_BUCKET_NAME", "vectors")
    def test_indexes_the_revision_publication_id(self):
        s3v = MagicMock()
        embedding = MagicMock()
        embedding.embed_document.return_value = [0.1]
        store = S3VectorsPlaybookStore(s3v, embedding)

        store.save_to_s3_vectors(
            {
                "playbook_id": "pb-1",
                "failure_type": "DB connection leak",
                "symptom_pattern": "커넥션 상승",
                "verification_status": "VERIFIED",
            },
            "rca-1",
            publication_id="exec-9",
        )

        metadata = s3v.put_vectors.call_args.kwargs["vectors"][0]["metadata"]
        assert metadata["publication_id"] == "exec-9"

    @patch(f"{_MODULE}.S3_VECTOR_BUCKET_NAME", "vectors")
    def test_an_unknown_status_is_indexed_as_a_draft(self):
        s3v = MagicMock()
        embedding = MagicMock()
        embedding.embed_document.return_value = [0.1]
        store = S3VectorsPlaybookStore(s3v, embedding)

        store.save_to_s3_vectors(
            {
                "playbook_id": "pb-1",
                "failure_type": "DB connection leak",
                "symptom_pattern": "커넥션 상승",
                "verification_status": "PROBABLY_FINE",
            },
            "rca-1",
        )

        metadata = s3v.put_vectors.call_args.kwargs["vectors"][0]["metadata"]
        assert metadata["verification_status"] == "DRAFT"
