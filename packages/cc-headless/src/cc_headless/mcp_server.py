"""격리 산출물 저장과 서버 검증형 Healthcare reset MCP server."""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib import error, request

import boto3
from fastmcp import FastMCP

from cc_headless.config.settings import (
    CLOUDWATCH_VERIFY_ATTEMPTS,
    CLOUDWATCH_VERIFY_INTERVAL_SECONDS,
    DYNAMODB_TABLE_NAME,
    ENGINE,
    HEALTHCARE_ECS_CLUSTER_NAME,
    HEALTHCARE_ECS_SERVICE_NAME,
    HEALTHCARE_RDS_INSTANCE_IDENTIFIER,
    HEALTHCARE_RESET_TIMEOUT_SECONDS,
    HEALTHCARE_SERVICE_HOST,
    SIDE_EFFECT_LEASE_SECONDS,
)
from cc_headless.ports.interfaces.session_store import SideEffectLeaseUnavailableError
from cc_headless.services.artifact_validation import (
    ArtifactValidationError,
    validate_artifact_shape,
    validate_remediation_evidence,
)
from cc_headless.services.execution_context import (
    CLAIM_TOKEN_ENV,
    RCA_ID_ENV,
    RUN_TOKEN_ENV,
    artifact_dir_for_token,
)
from cc_headless.services.post_reset_verification import verify_post_reset
from cc_headless.services.remediation_policy import (
    RESET_PATHS,
    HealthcareFaultType,
    parse_fault_type,
    validate_healthcare_alarm_target,
)

mcp = FastMCP("rca-progress")

_CANONICAL_ARTIFACTS = {
    "scoping.json",
    "hypotheses.json",
    "playbook.json",
    "report.md",
}
_VALIDATION_ARTIFACT = re.compile(r"validation-[1-9][0-9]*\.json")
_HEALTHCARE_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_MAX_RESET_RESPONSE_BYTES = 64 * 1024
_SUCCESSFUL_RESET_STATUSES = {"stopped", "not_running"}


class HealthcareResetResponseError(RuntimeError):
    pass


def _artifact_dir() -> Path | None:
    token = os.environ.get(RUN_TOKEN_ENV, "")
    try:
        artifact_dir = artifact_dir_for_token(token)
    except ValueError:
        return None
    return artifact_dir if artifact_dir.is_dir() and not artifact_dir.is_symlink() else None


def _is_allowed_filename(filename: str) -> bool:
    if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
        return False
    return filename in _CANONICAL_ARTIFACTS or _VALIDATION_ARTIFACT.fullmatch(filename) is not None


def _write_artifact(base: Path, filename: str, content: str) -> Path:
    path = base / filename
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=base,
            prefix=f".{filename}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return path


def _remediation_result(
    *,
    status: str,
    fault_type: str,
    reason: str,
    validation_artifact: str | None = None,
    confirmed_hypothesis_ids: list[str] | None = None,
    verification: dict | None = None,
) -> dict:
    parsed_fault_type = parse_fault_type(fault_type)
    endpoint_path = RESET_PATHS.get(parsed_fault_type) if status in {"SUCCEEDED", "FAILED"} else None
    result = {
        "stage": "REMEDIATION",
        "status": status,
        "fault_type": fault_type,
        "endpoint_path": endpoint_path,
        "validation_artifact": validation_artifact,
        "confirmed_hypothesis_ids": confirmed_hypothesis_ids or [],
        "summary": reason,
        "output_summary": f"{status}: {reason}",
        "verification": verification
        or {
            "status": "PENDING",
            "reason": "reset did not complete; post-reset verification was not run",
        },
    }
    if status != "SUCCEEDED":
        result["error"] = reason
    return result


def _save_remediation_result(base: Path, result: dict) -> str:
    _write_artifact(base, "remediation.json", json.dumps(result, ensure_ascii=False, indent=2))
    return _serialize_remediation_result(result)


def _serialize_remediation_result(result: dict) -> str:
    return json.dumps({"ok": result["status"] == "SUCCEEDED", **result}, ensure_ascii=False)


def _validate_reset_response(response) -> None:
    payload = response.read(_MAX_RESET_RESPONSE_BYTES + 1)
    if len(payload) > _MAX_RESET_RESPONSE_BYTES:
        raise HealthcareResetResponseError("response body exceeds size limit")
    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HealthcareResetResponseError("response body is not valid JSON") from exc
    if not isinstance(body, dict):
        raise HealthcareResetResponseError("response JSON must be an object")

    reset_status = body.get("status")
    if reset_status is not None and reset_status not in _SUCCESSFUL_RESET_STATUSES:
        raise HealthcareResetResponseError(f"non-success status: {reset_status}")


def _claim_context() -> tuple[str, str] | None:
    rca_id = os.environ.get(RCA_ID_ENV, "")
    claim_token = os.environ.get(CLAIM_TOKEN_ENV, "")
    if not rca_id or not claim_token:
        return None
    return rca_id, claim_token


def _session_store():
    from cc_headless.adapters.secondary.session.dynamodb_session_store import DynamoDbSessionStore

    ddb = boto3.client("dynamodb") if DYNAMODB_TABLE_NAME else None
    return DynamoDbSessionStore(ddb), ddb


def _load_claimed_alarm_data(ddb, rca_id: str, claim_token: str) -> dict:
    if not DYNAMODB_TABLE_NAME or not ddb:
        raise SideEffectLeaseUnavailableError("server-owned alarm data is unavailable")
    response = ddb.get_item(
        TableName=DYNAMODB_TABLE_NAME,
        Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not item or item.get("claim_token", {}).get("S") != claim_token:
        raise SideEffectLeaseUnavailableError("reset claim is no longer current")
    try:
        value = json.loads(item.get("alarm_data", {}).get("S", "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


@mcp.tool()
def save_artifact(filename: str, content: str) -> str:
    """분석 산출물을 현재 RCA 실행의 격리된 디렉터리에 저장한다.

    Args:
        filename: 파일명. JSON 산출물은 .json 확장자, 보고서는 report.md.
                  예: scoping.json, hypotheses.json, validation-1.json, report.md
        content: 파일 내용 (JSON 문자열 또는 마크다운).
    """
    if not _is_allowed_filename(filename):
        return json.dumps({"ok": False, "error": f"unsupported artifact filename: {filename}"})

    base = _artifact_dir()
    if base is None:
        return json.dumps({"ok": False, "error": "missing or invalid RCA execution context"})

    # Reject a malformed artifact now rather than at the completion gate, where
    # the run has already ended and the agent can no longer correct it.
    try:
        validate_artifact_shape(filename, content)
    except ArtifactValidationError as exc:
        return json.dumps(
            {"ok": False, "error": f"artifact rejected: {exc}. Fix the content and save again."},
            ensure_ascii=False,
        )

    path = _write_artifact(base, filename, content)
    return json.dumps({"ok": True, "path": str(path)})


@mcp.tool()
def execute_healthcare_reset(fault_type: str) -> str:
    """확정 RCA와 일치하는 Healthcare 장애 리셋만 실행한다.

    Args:
        fault_type: db-leak, high-cpu, high-memory, slow-query 중 하나.
                    매칭되는 allowlist action이 없으면 unsupported.
    """
    base = _artifact_dir()
    if base is None:
        return json.dumps({"ok": False, "error": "missing or invalid RCA execution context"})

    try:
        evidence = validate_remediation_evidence(base)
    except ArtifactValidationError as exc:
        return _serialize_remediation_result(
            _remediation_result(
                status="BLOCKED",
                fault_type=fault_type,
                reason=f"strict RCA validation failed: {exc}",
            ),
        )
    validation_artifact = evidence.validation_artifact
    confirmed_fault_type = evidence.fault_type
    hypothesis_ids = list(evidence.confirmed_hypothesis_ids)
    requested_fault_type = parse_fault_type(fault_type)

    if requested_fault_type is None:
        return _save_remediation_result(
            base,
            _remediation_result(
                status="BLOCKED",
                fault_type="unsupported",
                reason="confirmed root cause has no allowlisted remediation action",
                validation_artifact=validation_artifact,
                confirmed_hypothesis_ids=hypothesis_ids,
            ),
        )

    if confirmed_fault_type is HealthcareFaultType.UNSUPPORTED:
        return _save_remediation_result(
            base,
            _remediation_result(
                status="BLOCKED",
                fault_type=HealthcareFaultType.UNSUPPORTED,
                reason="confirmed fault type has no allowlisted remediation action",
                validation_artifact=validation_artifact,
                confirmed_hypothesis_ids=hypothesis_ids,
            ),
        )

    if requested_fault_type is not confirmed_fault_type:
        return _save_remediation_result(
            base,
            _remediation_result(
                status="BLOCKED",
                fault_type=fault_type,
                reason="requested action does not match the structured confirmed fault type",
                validation_artifact=validation_artifact,
                confirmed_hypothesis_ids=hypothesis_ids,
            ),
        )

    claim_context = _claim_context()
    if claim_context is None:
        return _save_remediation_result(
            base,
            _remediation_result(
                status="BLOCKED",
                fault_type=fault_type,
                reason="missing server claim context",
                validation_artifact=validation_artifact,
                confirmed_hypothesis_ids=hypothesis_ids,
            ),
        )
    rca_id, claim_token = claim_context
    try:
        store, ddb = _session_store()
        alarm_data = _load_claimed_alarm_data(ddb, rca_id, claim_token)
    except Exception as exc:
        return _save_remediation_result(
            base,
            _remediation_result(
                status="BLOCKED",
                fault_type=fault_type,
                reason=f"server-owned alarm data could not be validated: {type(exc).__name__}",
                validation_artifact=validation_artifact,
                confirmed_hypothesis_ids=hypothesis_ids,
            ),
        )

    target_error = validate_healthcare_alarm_target(
        alarm_data,
        requested_fault_type,
        ecs_cluster_name=HEALTHCARE_ECS_CLUSTER_NAME,
        ecs_service_name=HEALTHCARE_ECS_SERVICE_NAME,
        rds_instance_identifier=HEALTHCARE_RDS_INSTANCE_IDENTIFIER,
    )
    if target_error:
        return _save_remediation_result(
            base,
            _remediation_result(
                status="BLOCKED",
                fault_type=fault_type,
                reason=f"server-owned alarm target validation failed: {target_error}",
                validation_artifact=validation_artifact,
                confirmed_hypothesis_ids=hypothesis_ids,
            ),
        )
    if not HEALTHCARE_SERVICE_HOST or _HEALTHCARE_HOST.fullmatch(HEALTHCARE_SERVICE_HOST) is None:
        return _save_remediation_result(
            base,
            _remediation_result(
                status="FAILED",
                fault_type=fault_type,
                reason="Healthcare service host is not configured",
                validation_artifact=validation_artifact,
                confirmed_hypothesis_ids=hypothesis_ids,
            ),
        )

    try:
        lease_token = store.acquire_side_effect_lease(
            rca_id,
            claim_token=claim_token,
            effect_name="healthcare-reset",
            lease_seconds=SIDE_EFFECT_LEASE_SECONDS,
        )
    except Exception as exc:
        return _save_remediation_result(
            base,
            _remediation_result(
                status="BLOCKED",
                fault_type=fault_type,
                reason=f"current claim could not acquire reset lease: {type(exc).__name__}",
                validation_artifact=validation_artifact,
                confirmed_hypothesis_ids=hypothesis_ids,
            ),
        )

    try:
        endpoint_path = RESET_PATHS[requested_fault_type]
        reset_request = request.Request(
            f"http://{HEALTHCARE_SERVICE_HOST}:8000{endpoint_path}",
            data=b"",
            method="POST",
        )
        try:
            reset_completed_at: datetime | None = None
            with request.urlopen(reset_request, timeout=HEALTHCARE_RESET_TIMEOUT_SECONDS) as response:
                status_code = response.getcode()
                _validate_reset_response(response)
            if not 200 <= status_code < 300:
                raise RuntimeError(f"Healthcare reset returned HTTP {status_code}")
            reset_completed_at = datetime.now(UTC)
        except (error.HTTPError, error.URLError, OSError, RuntimeError) as exc:
            detail = str(exc) if isinstance(exc, HealthcareResetResponseError) else type(exc).__name__
            result = _remediation_result(
                status="FAILED",
                fault_type=fault_type,
                reason=f"Healthcare reset failed: {detail}",
                validation_artifact=validation_artifact,
                confirmed_hypothesis_ids=hypothesis_ids,
            )
        else:
            try:
                trigger = alarm_data.get("Trigger") if isinstance(alarm_data.get("Trigger"), dict) else {}
                can_query = bool(
                    trigger.get("Namespace")
                    and trigger.get("MetricName")
                    and trigger.get("Threshold") is not None
                    and trigger.get("ComparisonOperator")
                )
                region = alarm_data.get("Region") or os.environ.get("AWS_REGION") or "us-east-1"
                cloudwatch = boto3.client("cloudwatch", region_name=region) if can_query else None
                verification = verify_post_reset(
                    alarm_data,
                    cloudwatch,
                    attempts=CLOUDWATCH_VERIFY_ATTEMPTS,
                    interval_seconds=CLOUDWATCH_VERIFY_INTERVAL_SECONDS,
                    started_at=reset_completed_at,
                )
            except Exception as exc:
                verification = {
                    "status": "PENDING",
                    "reason": f"server post-reset verification unavailable: {type(exc).__name__}",
                }
            result = _remediation_result(
                status="SUCCEEDED",
                fault_type=fault_type,
                reason=f"Healthcare reset completed via {endpoint_path}",
                validation_artifact=validation_artifact,
                confirmed_hypothesis_ids=hypothesis_ids,
                verification=verification,
            )
        output = _save_remediation_result(base, result)
    finally:
        with suppress(Exception):
            store.release_side_effect_lease(
                rca_id,
                claim_token=claim_token,
                lease_token=lease_token,
            )

    return output
