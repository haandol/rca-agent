import inspect
import json
import os
import uuid
from contextlib import suppress
from pathlib import Path
from unittest.mock import Mock
from urllib import error

import pytest

from cc_headless import mcp_server
from cc_headless.adapters.secondary.session import dynamodb_session_store
from cc_headless.adapters.secondary.session.dynamodb_session_store import DynamoDbSessionStore
from cc_headless.services import execution_context
from cc_headless.services.execution_context import (
    CLAIM_TOKEN_ENV,
    RCA_ID_ENV,
    RUN_TOKEN_ENV,
    ExecutionContext,
)

HEALTHCARE_CLUSTER = "healthcare-cluster"
HEALTHCARE_SERVICE = "healthcare-service"
HEALTHCARE_DATABASE = "healthcare-db"


def _alarm_data_for_fault(fault_type: str) -> dict:
    targets = {
        "db-leak": (
            "AWS/RDS",
            "DatabaseConnections",
            [{"name": "DBInstanceIdentifier", "value": HEALTHCARE_DATABASE}],
        ),
        "slow-query": (
            "AWS/RDS",
            "ReadLatency",
            [{"name": "DBInstanceIdentifier", "value": HEALTHCARE_DATABASE}],
        ),
        "high-cpu": (
            "AWS/ECS",
            "CPUUtilization",
            [
                {"name": "ClusterName", "value": HEALTHCARE_CLUSTER},
                {"name": "ServiceName", "value": HEALTHCARE_SERVICE},
            ],
        ),
        "high-memory": (
            "AWS/ECS",
            "MemoryUtilization",
            [
                {"name": "ClusterName", "value": HEALTHCARE_CLUSTER},
                {"name": "ServiceName", "value": HEALTHCARE_SERVICE},
            ],
        ),
    }
    namespace, metric_name, dimensions = targets[fault_type]
    return {
        "AlarmName": f"healthcare-{fault_type}",
        "Region": "us-east-1",
        "Trigger": {
            "Namespace": namespace,
            "MetricName": metric_name,
            "Dimensions": dimensions,
        },
    }


def _ddb_session_item(alarm_data: dict, *, claim_token: str = "claim-token") -> dict:
    return {
        "Item": {
            "claim_token": {"S": claim_token},
            "alarm_data": {"S": json.dumps(alarm_data)},
        }
    }


@pytest.fixture
def artifact_home(monkeypatch, tmp_path):
    token = uuid.uuid4().hex
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path / "runs")
    monkeypatch.setenv(RUN_TOKEN_ENV, token)
    monkeypatch.setenv(RCA_ID_ENV, "rca-1")
    monkeypatch.setenv(CLAIM_TOKEN_ENV, "claim-token")
    context = ExecutionContext(rca_id="rca-1", token=token)
    base = context.prepare()
    store = Mock()
    store.acquire_side_effect_lease.return_value = "lease-token"
    ddb = Mock()
    ddb.get_item.return_value = _ddb_session_item(_alarm_data_for_fault("db-leak"))
    monkeypatch.setattr(mcp_server, "DYNAMODB_TABLE_NAME", "rca-sessions")
    monkeypatch.setattr(mcp_server, "HEALTHCARE_ECS_CLUSTER_NAME", HEALTHCARE_CLUSTER)
    monkeypatch.setattr(mcp_server, "HEALTHCARE_ECS_SERVICE_NAME", HEALTHCARE_SERVICE)
    monkeypatch.setattr(mcp_server, "HEALTHCARE_RDS_INSTANCE_IDENTIFIER", HEALTHCARE_DATABASE)
    monkeypatch.setattr(mcp_server, "_session_store", lambda: (store, ddb))
    yield base
    context.cleanup()


def _save_rejected(filename: str, content: str) -> bool:
    try:
        result = json.loads(mcp_server.save_artifact(filename, content))
    except (OSError, ValueError):
        return True
    return result.get("ok") is False


@pytest.mark.parametrize(
    "filename",
    [
        "../escaped.json",
        "../../escaped.json",
        "/tmp/escaped.json",
        "nested/report.md",
        r"..\escaped.json",
    ],
)
def test_save_artifact_rejects_path_traversal_and_nested_paths(artifact_home, filename):
    escaped = artifact_home.parent / "escaped.json"
    escaped.unlink(missing_ok=True)

    try:
        assert _save_rejected(filename, "{}") is True
        assert not escaped.exists()
    finally:
        escaped.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "filename",
    ["scoping.json", "hypotheses.json", "validation-1.json", "validation-99.json", "playbook.json", "report.md"],
)
def test_save_artifact_accepts_canonical_names(artifact_home, filename):
    content = "# report" if filename == "report.md" else "{}"

    result = json.loads(mcp_server.save_artifact(filename, content))

    assert result["ok"] is True
    assert Path(result["path"]) == artifact_home / filename
    assert (artifact_home / filename).read_text() == content


@pytest.mark.parametrize(
    "filename",
    ["notes.txt", "validation-1.md", "validation-x.json", "report.json", "scoping.md", ".hidden.json"],
)
def test_save_artifact_rejects_unknown_names_and_extensions(artifact_home, filename):
    assert _save_rejected(filename, "content") is True
    assert not (artifact_home / filename).exists()


def test_save_artifact_preserves_existing_file_when_atomic_replace_fails(artifact_home, monkeypatch):
    target = artifact_home / "report.md"
    target.write_text("stable report")

    def _replace_failure(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _replace_failure)
    with suppress(OSError):
        mcp_server.save_artifact("report.md", "new report")

    assert target.read_text() == "stable report"


@pytest.mark.parametrize("token", [None, "", "../escape", "g" * 32, "a" * 31, "a" * 33])
def test_save_artifact_rejects_missing_or_invalid_execution_token(monkeypatch, tmp_path, token):
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path / "runs")
    if token is None:
        monkeypatch.delenv(RUN_TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(RUN_TOKEN_ENV, token)

    result = json.loads(mcp_server.save_artifact("report.md", "must not be written"))

    assert result["ok"] is False
    assert not (tmp_path / "runs").exists()


def test_save_artifact_rejects_valid_token_without_prepared_run_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path / "runs")
    monkeypatch.setenv(RUN_TOKEN_ENV, uuid.uuid4().hex)

    result = json.loads(mcp_server.save_artifact("report.md", "must not be written"))

    assert result["ok"] is False


def test_save_artifact_rejects_symlinked_run_directory(monkeypatch, tmp_path):
    token = uuid.uuid4().hex
    artifact_root = tmp_path / "runs"
    outside = tmp_path / "outside"
    artifact_root.mkdir()
    outside.mkdir()
    artifact_root.joinpath(token).symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", artifact_root)
    monkeypatch.setenv(RUN_TOKEN_ENV, token)

    result = json.loads(mcp_server.save_artifact("report.md", "must not be written"))

    assert result["ok"] is False
    assert not outside.joinpath("report.md").exists()


def _write_rca_artifacts(
    artifact_home: Path,
    *,
    title: str,
    fault_type: str = "db-leak",
    confirmed: list[dict] | None = None,
    validation_index: int = 1,
) -> None:
    artifact_home.joinpath("hypotheses.json").write_text(
        json.dumps(
            {
                "stage": "HYPOTHESIS_GENERATION",
                "tree_id": "tree-1",
                "hypotheses": [
                    {
                        "hypothesis_id": "hypothesis-1",
                        "tree_id": "tree-1",
                        "title": title,
                        "description": f"{title} confirmed by metrics",
                        "fault_type": fault_type,
                        "category": "INFRASTRUCTURE",
                        "confidence_score": 0.9,
                        "required_evidence": ["metric"],
                        "status": "PENDING",
                        "parent_id": None,
                        "depth": 0,
                    }
                ],
                "summary": "one hypothesis",
                "output_summary": "one hypothesis generated",
            }
        )
    )
    artifact_home.joinpath(f"validation-{validation_index}.json").write_text(
        json.dumps(
            {
                "stage": "VALIDATION",
                "loop_index": validation_index,
                "confirmed": confirmed
                if confirmed is not None
                else [
                    {
                        "hypothesis_id": "hypothesis-1",
                        "confidence": 0.95,
                        "fault_type": fault_type,
                        "reasoning": title,
                    }
                ],
                "rejected": [],
                "needs_investigation": [],
                "closed": [],
                "new_hypotheses": [],
                "summary": "validation complete",
                "output_summary": "one confirmed hypothesis",
            }
        )
    )


class FakeHttpResponse:
    def __init__(self, status_code: int = 200, body: bytes = b'{"closed":1}'):
        self.status_code = status_code
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def getcode(self):
        return self.status_code

    def read(self, limit: int):
        self.limit = limit
        return self.body


@pytest.mark.parametrize(
    ("fault_type", "title", "endpoint"),
    [
        ("db-leak", "DB connection leak", "/fault/db-leak/reset"),
        ("high-cpu", "high CPU stress", "/fault/high-cpu/reset"),
        ("high-memory", "memory pressure OOM", "/fault/high-memory/reset"),
        ("slow-query", "slow query timeout", "/fault/slow-query/reset"),
    ],
)
def test_execute_healthcare_reset_allows_only_confirmed_fixed_endpoints(
    artifact_home,
    monkeypatch,
    fault_type,
    title,
    endpoint,
):
    _write_rca_artifacts(artifact_home, title=title, fault_type=fault_type)
    monkeypatch.setattr(
        mcp_server,
        "_load_claimed_alarm_data",
        Mock(return_value=_alarm_data_for_fault(fault_type)),
    )
    monkeypatch.setattr(mcp_server, "HEALTHCARE_SERVICE_HOST", "healthcare.internal")
    urlopen = Mock(return_value=FakeHttpResponse())
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset(fault_type))

    assert result["ok"] is True
    assert result["status"] == "SUCCEEDED"
    reset_request = urlopen.call_args.args[0]
    assert reset_request.full_url == f"http://healthcare.internal:8000{endpoint}"
    assert reset_request.method == "POST"
    stored = json.loads(artifact_home.joinpath("remediation.json").read_text())
    assert stored["endpoint_path"] == endpoint
    assert stored["validation_artifact"] == "validation-1.json"
    assert stored["verification"]["status"] == "PENDING"


@pytest.mark.parametrize(
    ("case", "alarm_data"),
    [
        (
            "non-healthcare namespace",
            {
                "AlarmName": "custom-db-connections",
                "Trigger": {
                    "Namespace": "Custom/Application",
                    "MetricName": "DatabaseConnections",
                    "Dimensions": [
                        {"name": "DBInstanceIdentifier", "value": HEALTHCARE_DATABASE},
                    ],
                },
            },
        ),
        (
            "same metric on another resource",
            {
                "AlarmName": "other-database-connections",
                "Trigger": {
                    "Namespace": "AWS/RDS",
                    "MetricName": "DatabaseConnections",
                    "Dimensions": [
                        {"name": "DBInstanceIdentifier", "value": "other-database"},
                    ],
                },
            },
        ),
        ("fault and metric mismatch", _alarm_data_for_fault("slow-query")),
        ("missing alarm data", {}),
    ],
)
def test_execute_healthcare_reset_blocks_untrusted_alarm_targets_before_side_effects(
    artifact_home,
    monkeypatch,
    case,
    alarm_data,
):
    _write_rca_artifacts(artifact_home, title="DB connection leak")
    store = Mock()
    monkeypatch.setattr(mcp_server, "_session_store", lambda: (store, Mock()))
    monkeypatch.setattr(mcp_server, "_load_claimed_alarm_data", Mock(return_value=alarm_data))
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "BLOCKED", case
    assert "alarm target validation failed" in result["error"]
    store.acquire_side_effect_lease.assert_not_called()
    urlopen.assert_not_called()


@pytest.mark.parametrize(
    ("fault_type", "setting_name"),
    [
        ("db-leak", "HEALTHCARE_RDS_INSTANCE_IDENTIFIER"),
        ("high-cpu", "HEALTHCARE_ECS_CLUSTER_NAME"),
        ("high-cpu", "HEALTHCARE_ECS_SERVICE_NAME"),
    ],
)
def test_execute_healthcare_reset_blocks_missing_expected_resource_configuration(
    artifact_home,
    monkeypatch,
    fault_type,
    setting_name,
):
    _write_rca_artifacts(artifact_home, title=fault_type, fault_type=fault_type)
    store = Mock()
    monkeypatch.setattr(mcp_server, "_session_store", lambda: (store, Mock()))
    monkeypatch.setattr(
        mcp_server,
        "_load_claimed_alarm_data",
        Mock(return_value=_alarm_data_for_fault(fault_type)),
    )
    monkeypatch.setattr(mcp_server, setting_name, "")
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset(fault_type))

    assert result["status"] == "BLOCKED"
    assert "target configuration is incomplete" in result["error"]
    store.acquire_side_effect_lease.assert_not_called()
    urlopen.assert_not_called()


def test_execute_healthcare_reset_signature_does_not_accept_url_or_endpoint():
    assert set(inspect.signature(mcp_server.execute_healthcare_reset).parameters) == {"fault_type"}


@pytest.mark.parametrize("reset_status", ["stopped", "not_running"])
def test_execute_healthcare_reset_accepts_explicit_success_statuses(
    artifact_home,
    monkeypatch,
    reset_status,
):
    _write_rca_artifacts(artifact_home, title="slow query timeout", fault_type="slow-query")
    monkeypatch.setattr(
        mcp_server,
        "_load_claimed_alarm_data",
        Mock(return_value=_alarm_data_for_fault("slow-query")),
    )
    monkeypatch.setattr(mcp_server, "HEALTHCARE_SERVICE_HOST", "healthcare.internal")
    response = FakeHttpResponse(body=json.dumps({"status": reset_status}).encode())
    monkeypatch.setattr(mcp_server.request, "urlopen", Mock(return_value=response))

    result = json.loads(mcp_server.execute_healthcare_reset("slow-query"))

    assert result["status"] == "SUCCEEDED"
    assert response.limit == mcp_server._MAX_RESET_RESPONSE_BYTES + 1


@pytest.mark.parametrize("reset_status", ["stop_timeout", "already_running", "failed"])
def test_execute_healthcare_reset_rejects_explicit_non_success_statuses(
    artifact_home,
    monkeypatch,
    reset_status,
):
    _write_rca_artifacts(artifact_home, title="slow query timeout", fault_type="slow-query")
    monkeypatch.setattr(
        mcp_server,
        "_load_claimed_alarm_data",
        Mock(return_value=_alarm_data_for_fault("slow-query")),
    )
    monkeypatch.setattr(mcp_server, "HEALTHCARE_SERVICE_HOST", "healthcare.internal")
    response = FakeHttpResponse(body=json.dumps({"status": reset_status}).encode())
    monkeypatch.setattr(mcp_server.request, "urlopen", Mock(return_value=response))

    result = json.loads(mcp_server.execute_healthcare_reset("slow-query"))

    assert result["status"] == "FAILED"
    assert result["ok"] is False
    assert f"non-success status: {reset_status}" in result["error"]
    stored = json.loads(artifact_home.joinpath("remediation.json").read_text())
    assert stored["status"] == "FAILED"


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"[]",
        b"{" + b"x" * (64 * 1024),
    ],
)
def test_execute_healthcare_reset_rejects_invalid_or_oversized_response(
    artifact_home,
    monkeypatch,
    body,
):
    _write_rca_artifacts(artifact_home, title="DB connection leak")
    monkeypatch.setattr(mcp_server, "HEALTHCARE_SERVICE_HOST", "healthcare.internal")
    monkeypatch.setattr(
        mcp_server.request,
        "urlopen",
        Mock(return_value=FakeHttpResponse(body=body)),
    )

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "FAILED"


def test_execute_healthcare_reset_blocks_unconfirmed_result_without_http(artifact_home, monkeypatch):
    _write_rca_artifacts(artifact_home, title="DB connection leak", confirmed=[])
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "BLOCKED"
    assert "no confirmed root cause" in result["error"]
    assert not artifact_home.joinpath("remediation.json").exists()
    urlopen.assert_not_called()


def test_execute_healthcare_reset_blocks_unresolved_competing_fault_before_side_effects(
    artifact_home,
    monkeypatch,
):
    _write_rca_artifacts(artifact_home, title="DB connection leak")
    hypotheses_path = artifact_home / "hypotheses.json"
    hypotheses = json.loads(hypotheses_path.read_text())
    hypotheses["hypotheses"].append(
        {
            "hypothesis_id": "hypothesis-2",
            "tree_id": "tree-1",
            "title": "High CPU",
            "description": "CPU saturation may be the root cause",
            "fault_type": "high-cpu",
            "category": "INFRASTRUCTURE",
            "confidence_score": 0.7,
            "required_evidence": ["metric"],
            "status": "PENDING",
            "parent_id": None,
            "depth": 0,
        }
    )
    hypotheses_path.write_text(json.dumps(hypotheses))
    validation_path = artifact_home / "validation-1.json"
    validation = json.loads(validation_path.read_text())
    validation["needs_investigation"] = [
        {
            "hypothesis_id": "hypothesis-2",
            "confidence": 0.7,
            "reasoning": "CPU evidence is still inconclusive",
        }
    ]
    validation_path.write_text(json.dumps(validation))
    lease_store = Mock()
    monkeypatch.setattr(mcp_server, "_session_store", lambda: (lease_store, Mock()))
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "BLOCKED"
    assert "unresolved competing hypothesis hypothesis-2" in result["error"]
    assert not artifact_home.joinpath("remediation.json").exists()
    lease_store.acquire_side_effect_lease.assert_not_called()
    urlopen.assert_not_called()


def test_execute_healthcare_reset_uses_latest_validation_and_fails_closed(artifact_home, monkeypatch):
    _write_rca_artifacts(artifact_home, title="DB connection leak", validation_index=1)
    artifact_home.joinpath("validation-2.json").write_text(
        json.dumps(
            {
                "stage": "VALIDATION",
                "loop_index": 2,
                "confirmed": [],
                "rejected": [],
                "needs_investigation": [],
                "closed": [],
                "new_hypotheses": [],
                "summary": "validation complete",
                "output_summary": "no confirmed hypothesis",
            }
        )
    )
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "BLOCKED"
    assert "validation-2.json has no confirmed root cause" in result["error"]
    assert not artifact_home.joinpath("remediation.json").exists()
    urlopen.assert_not_called()


def test_execute_healthcare_reset_blocks_malformed_latest_validation(artifact_home, monkeypatch):
    _write_rca_artifacts(artifact_home, title="DB connection leak", validation_index=1)
    artifact_home.joinpath("validation-2.json").write_text("{")
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "BLOCKED"
    assert "validation-2.json is not valid JSON" in result["error"]
    assert not artifact_home.joinpath("remediation.json").exists()
    urlopen.assert_not_called()


@pytest.mark.parametrize("fault_type", ["ecs-force-deploy", "http://attacker/reset", "../db-leak"])
def test_execute_healthcare_reset_blocks_unsupported_actions(artifact_home, monkeypatch, fault_type):
    _write_rca_artifacts(artifact_home, title="DB connection leak")
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset(fault_type))

    assert result["status"] == "BLOCKED"
    assert result["fault_type"] == "unsupported"
    assert result["endpoint_path"] is None
    assert result["validation_artifact"] == "validation-1.json"
    assert result["confirmed_hypothesis_ids"] == ["hypothesis-1"]
    stored = json.loads(artifact_home.joinpath("remediation.json").read_text())
    assert stored == {key: value for key, value in result.items() if key != "ok"}
    urlopen.assert_not_called()


def test_unsupported_action_does_not_create_remediation_for_unconfirmed_rca(artifact_home, monkeypatch):
    _write_rca_artifacts(
        artifact_home,
        title="custom application fault",
        fault_type="unsupported",
        confirmed=[],
    )
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("unsupported"))

    assert result["status"] == "BLOCKED"
    assert not artifact_home.joinpath("remediation.json").exists()
    urlopen.assert_not_called()


def test_execute_healthcare_reset_blocks_action_mismatch(artifact_home, monkeypatch):
    _write_rca_artifacts(artifact_home, title="DB connection leak")
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("high-cpu"))

    assert result["status"] == "BLOCKED"
    assert result["fault_type"] == "high-cpu"
    assert result["endpoint_path"] is None
    assert "structured confirmed fault type" in result["error"]
    stored = json.loads(artifact_home.joinpath("remediation.json").read_text())
    assert stored["endpoint_path"] is None
    urlopen.assert_not_called()


def test_execute_healthcare_reset_uses_structured_fault_type_instead_of_ambiguous_free_text(
    artifact_home,
    monkeypatch,
):
    _write_rca_artifacts(artifact_home, title="DB connection leak with high CPU stress")
    monkeypatch.setattr(mcp_server, "HEALTHCARE_SERVICE_HOST", "healthcare.internal")
    urlopen = Mock(return_value=FakeHttpResponse())
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "SUCCEEDED"
    assert urlopen.call_args.args[0].full_url.endswith("/fault/db-leak/reset")


def test_execute_healthcare_reset_blocks_low_confidence_confirmed_entry(artifact_home, monkeypatch):
    _write_rca_artifacts(
        artifact_home,
        title="DB connection leak",
        confirmed=[
            {
                "hypothesis_id": "hypothesis-1",
                "confidence": 0.79,
                "fault_type": "db-leak",
                "reasoning": "DB connection leak",
            }
        ],
    )
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "BLOCKED"
    assert not artifact_home.joinpath("remediation.json").exists()
    urlopen.assert_not_called()


def test_execute_healthcare_reset_rejects_untrusted_host_configuration(artifact_home, monkeypatch):
    _write_rca_artifacts(artifact_home, title="DB connection leak")
    monkeypatch.setattr(mcp_server, "HEALTHCARE_SERVICE_HOST", "healthcare.internal/attacker")
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "FAILED"
    assert "host is not configured" in result["error"]
    urlopen.assert_not_called()


def test_execute_healthcare_reset_records_failed_call_for_reporting(artifact_home, monkeypatch):
    _write_rca_artifacts(artifact_home, title="DB connection leak")
    monkeypatch.setattr(mcp_server, "HEALTHCARE_SERVICE_HOST", "healthcare.internal")
    monkeypatch.setattr(
        mcp_server.request,
        "urlopen",
        Mock(side_effect=error.URLError("service unavailable")),
    )

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "FAILED"
    assert result["ok"] is False
    stored = json.loads(artifact_home.joinpath("remediation.json").read_text())
    assert stored["status"] == "FAILED"
    assert "URLError" in stored["error"]


def test_save_artifact_cannot_overwrite_server_owned_remediation_result(artifact_home):
    result = json.loads(mcp_server.save_artifact("remediation.json", "{}"))

    assert result["ok"] is False
    assert not artifact_home.joinpath("remediation.json").exists()


def test_reset_holds_claim_lease_through_server_verification(artifact_home, monkeypatch):
    _write_rca_artifacts(artifact_home, title="DB connection leak")
    monkeypatch.setattr(mcp_server, "HEALTHCARE_SERVICE_HOST", "healthcare.internal")
    store = Mock()
    store.acquire_side_effect_lease.return_value = "lease-1"
    monkeypatch.setattr(mcp_server, "_session_store", lambda: (store, Mock()))
    monkeypatch.setattr(
        mcp_server,
        "_load_claimed_alarm_data",
        Mock(
            return_value={
                "Region": "us-east-1",
                "Trigger": {
                    "Namespace": "AWS/RDS",
                    "MetricName": "DatabaseConnections",
                    "Dimensions": [
                        {"name": "DBInstanceIdentifier", "value": HEALTHCARE_DATABASE},
                    ],
                    "Threshold": 30,
                    "ComparisonOperator": "GreaterThanThreshold",
                },
            }
        ),
    )
    cloudwatch = Mock()
    monkeypatch.setattr(mcp_server.boto3, "client", Mock(return_value=cloudwatch))
    verify = Mock(return_value={"status": "NORMALIZED", "reason": "metric normalized"})
    monkeypatch.setattr(mcp_server, "verify_post_reset", verify)
    monkeypatch.setattr(mcp_server.request, "urlopen", Mock(return_value=FakeHttpResponse()))

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["verification"]["status"] == "NORMALIZED"
    store.acquire_side_effect_lease.assert_called_once()
    verify.assert_called_once()
    store.release_side_effect_lease.assert_called_once_with(
        "rca-1",
        claim_token="claim-token",
        lease_token="lease-1",
    )


def test_reset_does_not_begin_http_after_claim_lease_rejection(artifact_home, monkeypatch):
    _write_rca_artifacts(artifact_home, title="DB connection leak")
    monkeypatch.setattr(mcp_server, "HEALTHCARE_SERVICE_HOST", "healthcare.internal")
    store = Mock()
    store.acquire_side_effect_lease.side_effect = RuntimeError("reclaimed")
    monkeypatch.setattr(mcp_server, "_session_store", lambda: (store, Mock()))
    monkeypatch.setattr(
        mcp_server,
        "_load_claimed_alarm_data",
        Mock(return_value=_alarm_data_for_fault("db-leak")),
    )
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "BLOCKED"
    urlopen.assert_not_called()


def test_reset_checks_server_claim_before_acquiring_side_effect_lease(artifact_home, monkeypatch):
    _write_rca_artifacts(artifact_home, title="DB connection leak")
    monkeypatch.setattr(mcp_server, "HEALTHCARE_SERVICE_HOST", "healthcare.internal")
    store = Mock()
    ddb = Mock()
    ddb.get_item.return_value = _ddb_session_item(
        _alarm_data_for_fault("db-leak"),
        claim_token="stale-claim",
    )
    monkeypatch.setattr(mcp_server, "_session_store", lambda: (store, ddb))
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "BLOCKED"
    assert "SideEffectLeaseUnavailableError" in result["error"]
    ddb.get_item.assert_called_once_with(
        TableName="rca-sessions",
        Key={"PK": {"S": "RCA#rca-1"}, "SK": {"S": "cc-headless#SESSION"}},
        ConsistentRead=True,
    )
    store.acquire_side_effect_lease.assert_not_called()
    urlopen.assert_not_called()


def test_execute_healthcare_reset_blocks_disagreement_between_structured_fault_fields(
    artifact_home,
    monkeypatch,
):
    _write_rca_artifacts(
        artifact_home,
        title="DB connection leak",
        fault_type="db-leak",
        confirmed=[
            {
                "hypothesis_id": "hypothesis-1",
                "confidence": 0.95,
                "fault_type": "high-cpu",
                "reasoning": "conflicting structured classification",
            }
        ],
    )
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "BLOCKED"
    assert "confirmed fault_type disagrees" in result["error"]
    urlopen.assert_not_called()


def test_execute_healthcare_reset_blocks_invalid_structured_fault_type(artifact_home, monkeypatch):
    _write_rca_artifacts(
        artifact_home,
        title="DB connection leak",
        fault_type="arbitrary-reset",
    )
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "BLOCKED"
    urlopen.assert_not_called()


@pytest.mark.parametrize(
    ("artifact_name", "mutation"),
    [
        ("hypotheses.json", lambda artifact: artifact.update(stage="WRONG")),
        ("hypotheses.json", lambda artifact: artifact["hypotheses"][0].pop("title")),
        ("hypotheses.json", lambda artifact: artifact["hypotheses"][0].update(confidence_score=1.01)),
        ("validation-1.json", lambda artifact: artifact.update(stage="WRONG")),
        ("validation-1.json", lambda artifact: artifact["confirmed"][0].update(confidence=1.01)),
        ("validation-1.json", lambda artifact: artifact["confirmed"][0].update(hypothesis_id="unknown")),
        ("validation-1.json", lambda artifact: artifact["confirmed"][0].update(fault_type="high-cpu")),
        ("validation-1.json", lambda artifact: artifact["new_hypotheses"].append({"hypothesis_id": "new"})),
    ],
)
def test_reset_strictly_validates_hypotheses_and_latest_validation_before_any_side_effect(
    artifact_home,
    monkeypatch,
    artifact_name,
    mutation,
):
    _write_rca_artifacts(artifact_home, title="DB connection leak")
    path = artifact_home / artifact_name
    artifact = json.loads(path.read_text())
    mutation(artifact)
    path.write_text(json.dumps(artifact))
    lease_store = Mock()
    monkeypatch.setattr(mcp_server, "_session_store", lambda: (lease_store, Mock()))
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "BLOCKED"
    lease_store.acquire_side_effect_lease.assert_not_called()
    urlopen.assert_not_called()


@pytest.mark.parametrize("table_name", ["", "rca-sessions"])
def test_reset_fails_closed_when_dynamodb_table_or_client_is_missing(
    artifact_home,
    monkeypatch,
    table_name,
):
    _write_rca_artifacts(artifact_home, title="DB connection leak")
    monkeypatch.setattr(mcp_server, "HEALTHCARE_SERVICE_HOST", "healthcare.internal")
    monkeypatch.setattr(mcp_server, "DYNAMODB_TABLE_NAME", table_name)
    monkeypatch.setattr(dynamodb_session_store, "DYNAMODB_TABLE_NAME", table_name)
    monkeypatch.setattr(
        mcp_server,
        "_session_store",
        lambda: (DynamoDbSessionStore(None), None),
    )
    urlopen = Mock()
    monkeypatch.setattr(mcp_server.request, "urlopen", urlopen)

    result = json.loads(mcp_server.execute_healthcare_reset("db-leak"))

    assert result["status"] == "BLOCKED"
    assert "SideEffectLeaseUnavailableError" in result["error"]
    urlopen.assert_not_called()
