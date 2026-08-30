"""대시보드의 취소·삭제가 실행 중 워커를 실제로 fencing 하는지 검증한다.

취소와 삭제는 사람이 시작하지만 워커가 소유한 세션 레코드에 쓴다. 두 작업이 claim
규칙 밖에 있으면 재전달된 메시지가 진행 중 실행과 나란히 도는 것을 막던 fencing이
사람 손으로 무력화된다.

이 테스트는 조건식 문자열을 비교하지 않고, 대시보드가 실제로 사용하는 조건식을 그대로
읽어와 저장소에 적용한 뒤 **엔진의 세션 저장소가 이후 쓰기를 거부하는지**를 본다.
조건식이 맞게 생겼는지가 아니라 fencing이 성립하는지가 확인 대상이다.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from codex_headless.adapters.secondary.session import dynamodb_session_store
from codex_headless.adapters.secondary.session.dynamodb_session_store import (
    DynamoDbSessionStore,
    SessionCancelledError,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FENCING_MODULE = REPOSITORY_ROOT / "packages/dashboard/server/utils/fencing.ts"

TABLE_NAME = "rca-sessions"
RCA_ID = "rca-1"


def _dashboard_fencing_expressions() -> dict:
    """대시보드가 배포하는 조건식을 모듈에서 직접 읽는다.

    조건식을 이 파일에 옮겨 적으면 대시보드가 조건을 약화시켜도 테스트는 계속
    통과한다. 실제 모듈을 실행해 얻어야 회귀를 잡는다.
    """
    script = (
        f"import {{ buildCancelUpdate, buildDeleteClaimUpdate }} from '{FENCING_MODULE}';\n"
        "console.log(JSON.stringify({\n"
        "  cancel: buildCancelUpdate('cancelled:operator', '2026-07-31T00:00:00.000Z', 1785196800),\n"
        "  delete: buildDeleteClaimUpdate('deleted:operator', '2026-07-31T00:00:00.000Z', 1785196800),\n"
        "}));\n"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
    )
    if result.returncode != 0:
        pytest.skip(f"dashboard fencing module could not be loaded: {result.stderr.strip()}")
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def fencing() -> dict:
    return _dashboard_fencing_expressions()


@pytest.fixture
def store(monkeypatch):
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(dynamodb_session_store, "DYNAMODB_TABLE_NAME", TABLE_NAME)
        yield DynamoDbSessionStore(ddb), ddb


def _to_attribute(value) -> dict:
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    return {"S": str(value)}


def _apply(ddb, expression: dict) -> None:
    """대시보드 조건식을 저장소에 그대로 적용한다."""
    ddb.update_item(
        TableName=TABLE_NAME,
        Key={"PK": {"S": f"RCA#{RCA_ID}"}, "SK": {"S": "codex-headless#SESSION"}},
        UpdateExpression=expression["UpdateExpression"],
        ConditionExpression=expression["ConditionExpression"],
        ExpressionAttributeNames=expression["ExpressionAttributeNames"],
        ExpressionAttributeValues={
            key: _to_attribute(value) for key, value in expression["ExpressionAttributeValues"].items()
        },
    )


def _analyzing_session(store: DynamoDbSessionStore) -> str:
    claim = store.claim_session(RCA_ID, "alarm", "idem", receive_count=1)
    store.update_state(RCA_ID, "ANALYZING", claim_token=claim.claim_token)
    return claim.claim_token


def test_cancel_stops_the_running_worker_from_writing(store, fencing):
    """취소 이후 진행 중 실행은 어떤 상태도 확정할 수 없다."""
    session_store, ddb = store
    claim_token = _analyzing_session(session_store)

    _apply(ddb, fencing["cancel"])

    # claim 이 회전했으므로 워커의 다음 쓰기는 모두 소유권 상실로 실패한다. 상태만
    # 바꾸는 취소였다면 이 호출들이 성공해 취소 이후에도 결과가 기록된다.
    with pytest.raises(SessionCancelledError):
        session_store.mark_completed(RCA_ID, "root cause", "reports/rca-1.md", claim_token=claim_token)
    with pytest.raises(SessionCancelledError):
        session_store.mark_failed(RCA_ID, "boom", claim_token=claim_token)
    with pytest.raises(SessionCancelledError):
        session_store.update_state(RCA_ID, "COMPLETED", claim_token=claim_token)

    assert session_store.is_terminated(RCA_ID, claim_token=claim_token) is True

    item = ddb.get_item(
        TableName=TABLE_NAME,
        Key={"PK": {"S": f"RCA#{RCA_ID}"}, "SK": {"S": "codex-headless#SESSION"}},
    )["Item"]
    assert item["state"]["S"] == "CANCELLED"
    assert item["claim_token"]["S"] != claim_token


def test_cancel_waits_for_an_active_side_effect_lease(store, fencing):
    """부작용이 lease 안에 있는 동안에는 취소가 성립하지 않는다."""
    session_store, ddb = store
    claim_token = _analyzing_session(session_store)
    session_store.acquire_side_effect_lease(RCA_ID, claim_token=claim_token, effect_name="report", lease_seconds=600)

    # 이미 시작된 외부 쓰기를 중간에서 끊으면 완료 여부를 알 수 없는 상태로 남는다.
    with pytest.raises(ClientError) as exc:
        _apply(ddb, fencing["cancel"])
    assert exc.value.response["Error"]["Code"] == "ConditionalCheckFailedException"

    # 취소가 거부됐으므로 워커는 자기 작업을 정상적으로 마칠 수 있다.
    session_store.mark_completed(RCA_ID, "root cause", "reports/rca-1.md", claim_token=claim_token)


def test_delete_is_refused_while_the_session_is_active(store, fencing):
    """활성 세션의 삭제는 fencing 기준 자체를 지우므로 거부된다."""
    session_store, ddb = store
    claim_token = _analyzing_session(session_store)

    with pytest.raises(ClientError) as exc:
        _apply(ddb, fencing["delete"])
    assert exc.value.response["Error"]["Code"] == "ConditionalCheckFailedException"

    # 삭제가 거부됐으니 진행 중 실행의 소유권은 그대로 유지된다.
    assert session_store.is_terminated(RCA_ID, claim_token=claim_token) is False


def test_delete_is_allowed_once_the_session_is_terminal(store, fencing):
    """최종 상태이고 lease 가 없으면 삭제가 성립한다."""
    session_store, ddb = store
    claim_token = _analyzing_session(session_store)
    session_store.mark_completed(RCA_ID, "root cause", "reports/rca-1.md", claim_token=claim_token)

    _apply(ddb, fencing["delete"])

    item = ddb.get_item(
        TableName=TABLE_NAME,
        Key={"PK": {"S": f"RCA#{RCA_ID}"}, "SK": {"S": "codex-headless#SESSION"}},
    )["Item"]
    # 삭제 직전 claim 을 회전시켜, 검사와 실제 삭제 사이에 옛 소유자가 레코드를
    # 되살리지 못하게 한다.
    assert item["claim_token"]["S"] != claim_token
    assert item["state"]["S"] == "COMPLETED"


def test_cancel_cannot_walk_back_a_published_report(store, fencing):
    """완료된 분석은 취소로 되돌리지 않는다 — 리포트는 이미 읽을 수 있다."""
    session_store, ddb = store
    claim_token = _analyzing_session(session_store)
    session_store.mark_completed(RCA_ID, "root cause", "reports/rca-1.md", claim_token=claim_token)

    with pytest.raises(ClientError) as exc:
        _apply(ddb, fencing["cancel"])
    assert exc.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
