"""Serve fictional incidents through local AWS-compatible services for browser checks.

Run with ``uv run --isolated --with 'moto[server]>=5,<6' python
tests/browser/playbook_fixture_server.py``. Point the dashboard's AWS_ENDPOINT_URL
at http://127.0.0.1:4569 with test credentials. DynamoDB transactions and S3 reads
use Moto; vectors and embeddings are deterministic test doubles, not model-quality
evidence. Nothing in this process authenticates against a real AWS endpoint.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

import boto3
from moto.server import DomainDispatcherApplication, create_backend_app
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages/agent/src"))

TABLE = "PlaybookBrowserFixture"
BUCKET = "playbook-browser-fixture"
PLAYBOOK_ID = "11111111-1111-4111-8111-111111111111"
SOURCE_ID = "22222222-2222-4222-8222-222222222222"
INCIDENT_ID = "33333333-3333-4333-8333-333333333333"
STALE_ID = "44444444-4444-4444-8444-444444444444"
PROPOSAL_ID = "55555555-5555-4555-8555-555555555555"
ENGINE = "strands"
VECTORS: dict[str, dict] = {}
CONTROL = {"fail_vector_once": False}
MOTO = DomainDispatcherApplication(create_backend_app)


def response(start_response, payload: object, status: str = "200 OK"):
    """Return a JSON response so SDK and fixture-control calls share HTTP framing."""
    body = json.dumps(payload, ensure_ascii=False).encode()
    start_response(
        status,
        [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
    )
    return [body]


def application(environ, start_response):
    """Keep storage semantics in Moto and isolate only unmodelled external services."""
    path = unquote(environ.get("PATH_INFO", ""))
    if path == "/__fixture__/health":
        return response(start_response, {"fixture": True})
    if path == "/__fixture__/fail-vector-once":
        CONTROL["fail_vector_once"] = True
        return response(start_response, {"armed": True})
    if path == "/__fixture__/vectors":
        return response(start_response, {"vectors": list(VECTORS.values())})
    if path in {
        "/ListVectors",
        "/GetVectors",
        "/PutVectors",
        "/DeleteVectors",
        "/QueryVectors",
    }:
        body = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
        request = json.loads(body or "{}")
        if path == "/PutVectors":
            if CONTROL["fail_vector_once"]:
                CONTROL["fail_vector_once"] = False
                return response(
                    start_response,
                    {
                        "__type": "ValidationException",
                        "message": "Deliberate local publication failure",
                    },
                    "400 Bad Request",
                )
            for vector in request.get("vectors", []):
                VECTORS[vector["key"]] = copy.deepcopy(vector)
            return response(start_response, {})
        if path == "/DeleteVectors":
            for key in request.get("keys", []):
                VECTORS.pop(key, None)
            return response(start_response, {})
        if path == "/GetVectors":
            return response(
                start_response,
                {
                    "vectors": [
                        VECTORS[k] for k in request.get("keys", []) if k in VECTORS
                    ]
                },
            )
        if path == "/QueryVectors":
            vectors = [{**v, "distance": 0.13} for v in VECTORS.values()]
            return response(
                start_response, {"vectors": vectors[: request.get("topK", 3)]}
            )
        offset = int(request.get("nextToken", "0"))
        # Deliberately return a short page with a token to exercise pagination.
        count = min(int(request.get("maxResults", 500)), 2)
        vectors = list(VECTORS.values())
        result = {"vectors": vectors[offset : offset + count]}
        if offset + count < len(vectors):
            result["nextToken"] = str(offset + count)
        return response(start_response, result)
    if path.startswith("/model/") and path.endswith("/invoke"):
        return response(start_response, {"embeddings": {"float": [[0.1, 0.2, 0.3]]}})
    return MOTO(environ, start_response)


def playbook() -> dict:
    """Create fictional knowledge and a historical command whose target must not leak."""
    return {
        "playbook_id": PLAYBOOK_ID,
        "failure_type": "데이터베이스 연결 풀 고갈",
        "symptom_pattern": "요청 대기와 연결 사용량이 함께 증가",
        "severity_criteria": "수집 실패가 이어지면 high",
        "verification_steps": ["연결 사용량과 대기 요청을 같은 시간대에 확인"],
        "execution_steps": [
            {
                "step_id": "observe",
                "intent": "과거 사고 관측",
                "action": "기록된 알람 상태 조회",
                "commands": [
                    "aws cloudwatch describe-alarms --region us-east-1 --alarm-names historical-fixture"
                ],
                "metric_wait": None,
                "success_criteria": "기록된 알람 상태를 확인한다",
            }
        ],
        "temporary_mitigation": "소유자가 확인된 서비스의 복구 절차를 검토한다",
        "permanent_remediation": "요청 종료 시 연결을 반환한다",
        "escalation_criteria": "소유권을 확인할 수 없으면 담당자에게 전달",
        "prevention_measures": ["연결 반환 경로를 검증"],
        "related_metrics": ["DatabaseConnections"],
        "tags": ["database", "pool"],
        "rca_id": SOURCE_ID,
        "verification_status": "VERIFIED",
    }


def comparison(before: dict) -> dict:
    """Provide an immutable, evidence-backed pending proposal for the browser flow."""
    after = copy.deepcopy(before)
    after["prevention_measures"].append("요청 취소 경로에서도 연결 반환을 검증")
    return {
        "comparison_id": "fixture-comparison",
        "status": "UPDATE_PROPOSED",
        "query": "장애유형: 데이터베이스 연결 풀 고갈 | 증상: 요청 대기와 연결 사용량이 함께 증가",
        "selected_playbook_id": PLAYBOOK_ID,
        "baseline": {
            "playbook_id": PLAYBOOK_ID,
            "revision": f"analysis:{SOURCE_ID}",
            "source_rca_id": SOURCE_ID,
            "source_engine": ENGINE,
            "playbook": copy.deepcopy(before),
        },
        "inputs": {
            "current_report": {
                "rca_id": INCIDENT_ID,
                "root_cause": "요청 취소 경로의 연결 반환 누락",
                "evidence_list": ["취소 요청 뒤 연결이 반환되지 않은 테스트 로그"],
            }
        },
        "used_references": [
            {
                "role": "knowledge-reuse",
                "ref": "baseline",
                "playbook_id": PLAYBOOK_ID,
                "revision": f"analysis:{SOURCE_ID}",
                "source_rca_id": SOURCE_ID,
                "source_engine": ENGINE,
            },
            {
                "role": "current-runbook-input",
                "ref": "current_report",
                "source_rca_id": INCIDENT_ID,
                "source_engine": ENGINE,
            },
            {
                "role": "comparison-evidence",
                "ref": "fixture-evidence-h1",
                "source_rca_id": INCIDENT_ID,
                "source_engine": ENGINE,
            },
        ],
        "evidence": [
            {
                "ref": "fixture-evidence-h1",
                "value": {
                    "message": "취소 요청 뒤 연결이 반환되지 않은 테스트 로그",
                    "observed_at": "2026-09-15T01:02:00Z",
                },
                "source_rca_id": INCIDENT_ID,
                "source_engine": ENGINE,
            }
        ],
        "candidates": [
            {
                "playbook_id": PLAYBOOK_ID,
                "rca_id": SOURCE_ID,
                "engine": ENGINE,
                "similarity": 0.87,
                "revision": f"analysis:{SOURCE_ID}",
                "availability": "AVAILABLE",
                "applicable": True,
                "rationale": "두 사고 모두 연결 반환 누락과 요청 대기가 관측되었다",
            }
        ],
        "proposal": {
            "proposal_id": PROPOSAL_ID,
            "playbook_id": PLAYBOOK_ID,
            "base_revision": f"analysis:{SOURCE_ID}",
            "source_rca_id": SOURCE_ID,
            "source_engine": ENGINE,
            "before": copy.deepcopy(before),
            "after": after,
            "changes": [
                {
                    "field": "prevention_measures",
                    "before": before["prevention_measures"],
                    "after": after["prevention_measures"],
                }
            ],
            "rationale": "이번 사고에서 취소된 요청의 연결 반환 누락을 확인했다",
            "evidence": ["fixture-evidence-h1"],
            "state": "PENDING",
        },
    }


REPORT = """# 테스트용 장애 분석 리포트

## 빠른 판단
- **장애**: 테스트 수집 요청 대기
- **영향**: 테스트 수집 서비스의 일부 요청 지연
- **심각도**: high
- **원인**: 요청 취소 경로의 연결 반환 누락
- **확정 상태**: 확정
- **신뢰도**: 0.94
- **다음 조치**: 현재 사고의 알람 관측 런북을 검토한다

## Incident Summary
이 문서는 로컬 브라우저 검증을 위한 가상 장애 자료다.
취소된 요청이 연결을 반환하지 않아 연결 풀이 고갈되었다.

## Impact Assessment
테스트 수집 서비스의 일부 요청이 지연되었다.

## Root Cause
요청 취소 경로의 연결 반환 누락
- **Status**: CONFIRMED
- **Confidence**: 0.94

## 5 Whys
1. 요청이 지연되었다 → 연결 대기가 증가했다.
2. 연결 대기가 증가했다 → 반환되지 않은 연결이 쌓였다.
3. 연결이 반환되지 않았다 → 요청 취소 경로에 반환 처리가 없었다.

## Evidence
- fixture-evidence-h1: 취소 요청 뒤 연결이 반환되지 않은 테스트 로그

## Timeline
- 2026-09-15T01:00:00Z — 테스트 알람 발생
- 2026-09-15T01:02:00Z — 연결 반환 누락 확인

## Playbook
연결 사용량과 요청 대기를 함께 확인한다.

## Action Items
- 취소 요청의 연결 반환 회귀 테스트 추가

## Lessons Learned
정상 종료 경로뿐 아니라 취소 경로도 검증해야 한다.
"""


def seed(endpoint: str) -> None:
    """Seed only the explicitly supplied loopback endpoint, including legacy records."""
    from rca_agent.adapters.secondary.playbook.library import (
        PlaybookLibrary,
        state_record,
    )

    if not endpoint.startswith("http://127.0.0.1:"):
        raise ValueError("Fixture endpoint must be loopback")
    session = boto3.Session(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        region_name="us-east-1",
    )
    ddb = session.resource("dynamodb", endpoint_url=endpoint)
    table = ddb.create_table(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "list_engine", "AttributeType": "S"},
            {"AttributeName": "list_created_at", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": "session-by-engine-index",
                "KeySchema": [
                    {"AttributeName": "list_engine", "KeyType": "HASH"},
                    {"AttributeName": "list_created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    library = PlaybookLibrary(session.client("dynamodb", endpoint_url=endpoint), TABLE)
    s3 = session.client("s3", endpoint_url=endpoint)
    s3.create_bucket(Bucket=BUCKET)
    before = playbook()
    current = copy.deepcopy(before)
    current["rca_id"] = INCIDENT_ID
    current["verification_status"] = "DRAFT"
    current["library_revision"] = f"analysis:{SOURCE_ID}"
    current["source_rca_id"] = SOURCE_ID
    current["source_engine"] = ENGINE
    current["comparison"] = comparison(before)
    current["execution_steps"][0]["commands"] = [
        "aws cloudwatch describe-alarms --region us-east-1 --alarm-names current-fixture"
    ]
    current["execution_steps"][0]["intent"] = "현재 사고 관측"
    current["comparison"]["inputs"]["current_playbook"] = {
        key: copy.deepcopy(value)
        for key, value in current.items()
        if key != "comparison"
    }
    now = datetime.now(UTC).isoformat()
    ttl = int(time.time()) + 90 * 86400
    for rca_id, pb in (
        (SOURCE_ID, before),
        (INCIDENT_ID, current),
        (STALE_ID, current),
    ):
        pb = copy.deepcopy(pb)
        pb["rca_id"] = rca_id
        if rca_id == STALE_ID:
            pb["comparison"]["proposal"]["proposal_id"] = (
                "66666666-6666-4666-8666-666666666666"
            )
        if pb.get("comparison"):
            pb["comparison"]["inputs"]["current_report"]["rca_id"] = rca_id
            for reference in pb["comparison"]["used_references"]:
                if reference["role"] != "knowledge-reuse":
                    reference["source_rca_id"] = rca_id
            table.put_item(
                Item={
                    "PK": f"RCA#{rca_id}",
                    "SK": "ANALYSIS#SESSION",
                    "engine": ENGINE,
                    "state": "PLAYBOOK_GENERATION",
                    "created_at": now,
                    "ttl": ttl,
                }
            )
            pb = library.archive_comparison(pb, rca_id, ENGINE)
        key = f"reports/{ENGINE}/{rca_id}.md"
        table.put_item(
            Item={
                "PK": f"RCA#{rca_id}",
                "SK": "ANALYSIS#SESSION",
                "rca_id": rca_id,
                "engine": ENGINE,
                "state": "COMPLETED",
                "alarm_name": "테스트 수집 요청 대기",
                "root_cause": "요청 취소 경로의 연결 반환 누락",
                "confirmed": True,
                "playbook_id": PLAYBOOK_ID,
                "playbook_span_id": "playbook-fixture",
                "playbook": json.dumps(pb, ensure_ascii=False),
                "completion_playbook": json.dumps(pb, ensure_ascii=False),
                "playbook_index_status": "PUBLISHED",
                "notification_status": "SENT",
                "report_s3_key": key,
                "created_at": now,
                "updated_at": now,
                "list_engine": ENGINE,
                "list_created_at": now + rca_id,
                "ttl": ttl,
            }
        )
        table.put_item(
            Item={
                "PK": f"RCA#{rca_id}",
                "SK": f"{ENGINE}#SPAN#playbook-fixture",
                "span_type": "PLAYBOOK",
                "span_status": "COMPLETED",
                "engine": ENGINE,
                "start_time": now,
                "end_time": now,
                "metadata": json.loads(json.dumps(pb), parse_float=Decimal),
                "ttl": ttl,
            }
        )
        s3.put_object(
            Bucket=BUCKET, Key=key, Body=REPORT.encode(), ContentType="text/markdown"
        )
    revision = f"analysis:{SOURCE_ID}"
    vector_key = f"{PLAYBOOK_ID}@{revision}"
    head = {
        "PK": "PLAYBOOK_LIBRARY",
        "SK": PLAYBOOK_ID,
        "playbook_json": json.dumps(before, ensure_ascii=False),
        "revision": revision,
        "source_rca_id": SOURCE_ID,
        "engine": ENGINE,
        "publication_status": "PUBLISHED",
        "vector_key": vector_key,
        "metric_name": "DatabaseConnections",
        "updated_at": now,
        "original_created_at": int(time.time()),
        "ttl": int(time.time()) + 60 * 86400,
    }
    table.put_item(Item=head)
    table.put_item(Item={**head, "PK": f"PLAYBOOK#{PLAYBOOK_ID}", "SK": revision})
    table.put_item(Item=state_record(head))
    VECTORS[vector_key] = {
        "key": vector_key,
        "data": {"float32": [0.1, 0.2, 0.3]},
        "metadata": {
            "playbook_id": PLAYBOOK_ID,
            "library_revision": revision,
            "rca_id": SOURCE_ID,
            "engine": ENGINE,
            "verification_status": "VERIFIED",
            "failure_type": before["failure_type"],
            "symptom_pattern": before["symptom_pattern"],
            "tags": "database,pool",
        },
    }


def main() -> None:
    """Start one disposable service instance; the fixture never touches user browsers."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4569)
    args = parser.parse_args()
    server = make_server("127.0.0.1", args.port, application, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{args.port}"
    seed(endpoint)
    print(
        json.dumps(
            {
                "endpoint": endpoint,
                "table": TABLE,
                "bucket": BUCKET,
                "incident": INCIDENT_ID,
                "staleIncident": STALE_ID,
                "fixture": "fictional; no live AWS/model validation",
            }
        ),
        flush=True,
    )
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
