"""Offline AWS contracts for the narrow RCA observation surface."""

import json
import tomllib
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import Mock

import boto3
import pytest
from botocore.stub import Stubber

from headless_codex import mcp_server
from headless_codex.adapters.secondary.codex.codex_harness import (
    _PROFILE_CONFIG,
    prepare_codex_home,
)
from headless_codex.adapters.secondary.evidence.ecs_control import (
    EcsControlError,
    alarm_scope,
    inspect_task,
    validate_target,
)
from headless_codex.adapters.secondary.session import dynamodb_session_store
from headless_codex.ports.dto.models import AlarmContext
from headless_codex.services import execution_context
from headless_codex.services.analysis_contract import replay_analysis
from headless_codex.services.prompt_builder import build_prompt

SCOPE = ("aws", "us-east-1", "123456789012")
PREFIX = "arn:aws:ecs:us-east-1:123456789012:"
CLUSTER = PREFIX + "cluster/healthcare"
TASK = PREFIX + "task/healthcare/" + "a" * 32
DEFINITION = PREFIX + "task-definition/maintenance:7"
ALARM = {
    "AlarmArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:ingest",
    "AWSAccountId": "123456789012",
    "Region": "us-east-1",
}
TASK_PARAMS = {"cluster": CLUSTER, "tasks": [TASK], "include": ["TAGS"]}
NOW = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_registered_tool_is_read_only_and_has_only_observed_identity_inputs():
    tool = await mcp_server.mcp.get_tool("inspect_ecs_task_control")
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert set(tool.parameters["properties"]) == {"task_arn", "cluster_arn"}
    assert set(tool.parameters["required"]) == {"task_arn", "cluster_arn"}


@pytest.fixture
def ecs():
    # Static dummy credentials prevent IMDS/credential discovery. All calls below
    # are intercepted by Stubber, including rejection of any unexpected write.
    return boto3.client("ecs", region_name=SCOPE[1], aws_access_key_id="testing", aws_secret_access_key="testing")


@pytest.fixture
def task_response():
    return {
        "tasks": [
            {
                "taskArn": TASK,
                "clusterArn": CLUSTER,
                "taskDefinitionArn": DEFINITION,
                "group": "family:maintenance",
                "lastStatus": "RUNNING",
                "desiredStatus": "RUNNING",
                "startedBy": "demo-journal-hash",
                "createdAt": NOW,
                "startedAt": NOW,
                "tags": [
                    {"key": "RealisticDemoRunId", "value": "owned-run-1"},
                    {"key": "RealisticDemoJournal", "value": "b" * 64},
                    {"key": "Password", "value": "tag-plaintext-do-not-return"},
                    {"key": "owner", "value": "postgres://user:embedded-credential@host/db"},
                    {"key": "Description", "value": "unknown-free-text-secret"},
                ],
                "overrides": {
                    "containerOverrides": [
                        {
                            "name": "maintenance",
                            "command": ["--password", "override-command-secret"],
                            "environment": [{"name": "DB_PASSWORD", "value": "override-env-secret"}],
                        }
                    ]
                },
                "containers": [
                    {
                        "name": "maintenance",
                        "image": "repo/maintenance:7",
                        "imageDigest": "sha256:" + "c" * 64,
                        "lastStatus": "RUNNING",
                        "reason": "unprojected-container-reason",
                    }
                ],
            }
        ],
        "failures": [],
    }


def add_success(stub, task_response, *, task_params=None):
    stub.add_response("describe_tasks", task_response, task_params or TASK_PARAMS)


@pytest.mark.parametrize(
    ("group", "kind"),
    [
        ("family:maintenance", "standalone"),
        ("service:ingest", "service"),
        ("custom-run-group", "unknown"),
        ("", "unknown"),
        ("family:other", "unknown"),
    ],
)
def test_projection_identity_group_and_secret_redaction(ecs, task_response, group, kind):
    task_response["tasks"][0]["group"] = group
    with Stubber(ecs) as stub:
        add_success(stub, task_response)
        result = inspect_task(ecs, TASK, CLUSTER, SCOPE)
        stub.assert_no_pending_responses()
    task = result["task"]
    assert task["task_arn"] == TASK
    assert task["cluster_arn"] == CLUSTER
    assert task["launch_kind"] == kind
    assert task["last_status"] == "RUNNING"
    assert task["started_by"] == "demo-journal-hash"
    assert task["times"]["startedAt"] == NOW.isoformat()
    assert datetime.fromisoformat(result["observed_at"]).tzinfo is not None
    assert task["containers"][0]["image_digest"] == "sha256:" + "c" * 64
    tags = {t["key"]: t["value"] for t in task["tags"]}
    assert tags["RealisticDemoRunId"] == "owned-run-1"
    assert tags["RealisticDemoJournal"] == "b" * 64
    assert tags["owner"] == "[REDACTED]"
    assert task["task_definition_arn"] == DEFINITION
    assert task["task_definition_arn_derived"] == {"family": "maintenance", "revision": 7}
    assert task["containers"][0]["image"] == "repo/maintenance:7"
    assert "task_definition" not in result
    serialized = json.dumps(result)
    for forbidden in (
        "tag-plaintext-do-not-return",
        "embedded-credential",
        "unknown-free-text-secret",
        "override-command-secret",
        "override-env-secret",
        "unprojected-container-reason",
    ):
        assert forbidden not in serialized
    for forbidden_key in ("environment", "secrets", "command", "entryPoint", "overrides"):
        assert f'"{forbidden_key}":' not in serialized


def test_stopped_task_is_observed_without_asserting_rollback(ecs, task_response):
    task = task_response["tasks"][0]
    task.update(lastStatus="STOPPED", stoppedAt=NOW)
    del task["tags"]
    with Stubber(ecs) as stub:
        add_success(stub, task_response)
        result = inspect_task(ecs, TASK, CLUSTER, SCOPE)
    assert result["task"]["last_status"] == "STOPPED"
    assert result["task"]["times"]["stoppedAt"] == NOW.isoformat()
    assert result["task"]["tags"] is None
    assert "not alarm-window state or proof of DB ownership/rollback" in result["limitations"][0]


@pytest.mark.parametrize(
    ("task", "cluster"),
    [
        ("a" * 32, CLUSTER),
        (TASK, "healthcare"),
        (TASK.replace("healthcare/", "another/"), CLUSTER),
        (TASK.replace("123456789012", "999999999999"), CLUSTER),
        (TASK.replace("us-east-1", "us-west-2"), CLUSTER),
        (TASK, CLUSTER.replace("123456789012", "999999999999")),
        (TASK, CLUSTER.replace("us-east-1", "us-west-2")),
        (TASK.replace("arn:aws:", "arn:aws-cn:"), CLUSTER),
        (TASK + "\n", CLUSTER),
        (TASK.replace("ecs:", "s3:"), CLUSTER),
    ],
)
def test_invalid_or_cross_scope_targets_never_reach_aws(ecs, task, cluster):
    with Stubber(ecs), pytest.raises(EcsControlError):
        inspect_task(ecs, task, cluster, SCOPE)


def test_client_region_mismatch_never_calls_aws():
    wrong_region = Mock()
    wrong_region.meta.region_name = "us-west-2"
    with pytest.raises(EcsControlError, match="client region mismatch"):
        inspect_task(wrong_region, TASK, CLUSTER, SCOPE)
    assert wrong_region.mock_calls == []


def test_legacy_task_arn_still_requires_response_cluster_match(ecs, task_response):
    task_arn = PREFIX + "task/" + "a" * 32
    task_response["tasks"][0]["taskArn"] = task_arn
    with Stubber(ecs) as stub:
        add_success(stub, task_response, task_params={**TASK_PARAMS, "tasks": [task_arn]})
        assert inspect_task(ecs, task_arn, CLUSTER, SCOPE)["task"]["task_arn"] == task_arn


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("taskArn", TASK.replace("a" * 32, "b" * 32)),
        ("clusterArn", CLUSTER + "-other"),
        ("taskDefinitionArn", DEFINITION.replace("123456789012", "999999999999")),
        ("taskDefinitionArn", DEFINITION.replace("us-east-1", "us-west-2")),
        ("taskDefinitionArn", "maintenance:7"),
    ],
)
def test_mismatched_task_response_is_rejected(ecs, task_response, field, value):
    task_response["tasks"][0][field] = value
    with Stubber(ecs) as stub:
        stub.add_response("describe_tasks", task_response, TASK_PARAMS)
        with pytest.raises(EcsControlError):
            inspect_task(ecs, TASK, CLUSTER, SCOPE)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "response",
    [
        {"tasks": []},
        {"tasks": [], "failures": [{"arn": TASK, "reason": "MISSING", "detail": "private-detail"}]},
    ],
)
def test_missing_task_and_failures_are_not_success(ecs, response):
    with Stubber(ecs) as stub:
        stub.add_response("describe_tasks", response, TASK_PARAMS)
        with pytest.raises(EcsControlError, match="missing or failed"):
            inspect_task(ecs, TASK, CLUSTER, SCOPE)


def test_task_access_denied_does_not_expose_aws_error_text(ecs):
    with Stubber(ecs) as stub:
        stub.add_client_error(
            "describe_tasks", "AccessDeniedException", "sensitive-aws-message", expected_params=TASK_PARAMS
        )
        with pytest.raises(EcsControlError) as caught:
            inspect_task(ecs, TASK, CLUSTER, SCOPE)
    assert "sensitive-aws-message" not in str(caught.value)


def test_only_describe_tasks_is_called_and_definition_is_not_presented_as_queried(ecs, task_response, monkeypatch):
    request = Mock(wraps=ecs._make_api_call)
    monkeypatch.setattr(ecs, "_make_api_call", request)
    with Stubber(ecs) as stub:
        add_success(stub, task_response)
        result = inspect_task(ecs, TASK, CLUSTER, SCOPE)
        stub.assert_no_pending_responses()
    # Record every SDK operation, including attempts a future implementation
    # might catch/ignore. No definition, separate tag, or write operation is allowed.
    request.assert_called_once_with("DescribeTasks", TASK_PARAMS)
    assert result["task"]["task_definition_arn"] == DEFINITION
    assert result["task"]["task_definition_arn_derived"] == {"family": "maintenance", "revision": 7}
    assert (
        "Task definition not queried; family/revision are parsed only from taskDefinitionArn." in result["limitations"]
    )
    assert set(result) == {"observed_at", "task", "limitations"}
    assert not {"status", "registeredAt", "deregisteredAt", "containerDefinitions"} & set(result["task"])


@pytest.mark.parametrize(
    "alarm",
    [
        {},
        {"Region": "us-east-1"},
        {**ALARM, "AWSAccountId": "999999999999"},
        {**ALARM, "Region": "us-west-2"},
        {**ALARM, "AlarmArn": "invalid"},
        {**ALARM, "AWSAccountId": 123456789012},
        {"AWSAccountId": SCOPE[2]},
    ],
)
def test_account_region_come_from_validated_session_alarm(alarm):
    with pytest.raises(ValueError):
        alarm_scope(alarm)


def test_alarm_scope_supports_arn_and_persisted_account_without_sts():
    assert alarm_scope(ALARM) == SCOPE
    assert alarm_scope({k: v for k, v in ALARM.items() if k != "AlarmArn"}) == SCOPE
    assert alarm_scope({**ALARM, "Region": "US East (N. Virginia)"}) == SCOPE
    gov_scope = ("aws-us-gov", "us-gov-west-1", SCOPE[2])
    validate_target(
        TASK.replace("aws:ecs:us-east-1", "aws-us-gov:ecs:us-gov-west-1"),
        CLUSTER.replace("aws:ecs:us-east-1", "aws-us-gov:ecs:us-gov-west-1"),
        gov_scope,
    )


@pytest.fixture
def runtime(monkeypatch, tmp_path, ecs):
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path)
    context = execution_context.ExecutionContext.create("rca-test")
    base = context.prepare()
    monkeypatch.setenv("RCA_EXECUTION_TOKEN", context.token)
    monkeypatch.setenv("RCA_SESSION_ID", "rca-test")
    monkeypatch.setenv("RCA_CLAIM_TOKEN", "claim-test")
    monkeypatch.setenv("RCA_ECS_CONTROL_DISCOVERY", "1")
    monkeypatch.setattr(mcp_server.settings, "DYNAMODB_TABLE_NAME", "sessions")
    monkeypatch.setattr(dynamodb_session_store, "DYNAMODB_TABLE_NAME", "sessions")
    item = {
        "claim_token": {"S": "claim-test"},
        "state": {"S": "EVIDENCE_COLLECTION"},
        "alarm_data": {"S": json.dumps(ALARM)},
    }
    ddb = Mock(spec_set=["get_item"])
    ddb.get_item.return_value = {"Item": item}
    calls = []

    def client(service, **kwargs):
        calls.append((service, kwargs))
        assert service in {"ecs", "dynamodb"}
        return ecs if service == "ecs" else ddb

    monkeypatch.setattr(mcp_server.boto3, "client", client)
    return item, ddb, calls, base


def test_mcp_checks_owned_active_session_before_and_after_read_without_writes(ecs, task_response, runtime):
    _, ddb, calls, base = runtime
    with Stubber(ecs) as stub:
        add_success(stub, task_response)
        result = json.loads(mcp_server.inspect_ecs_task_control(TASK, CLUSTER))
        stub.assert_no_pending_responses()
    assert result["ok"]
    assert len(ddb.mock_calls) == 2
    ddb.get_item.assert_called_with(
        TableName="sessions",
        Key={"PK": {"S": "RCA#rca-test"}, "SK": {"S": "ANALYSIS#SESSION"}},
        ConsistentRead=True,
    )
    assert [c[0] for c in calls] == ["dynamodb", "ecs"]
    config = calls[1][1]["config"]
    assert config.connect_timeout == 5
    assert config.read_timeout == 10
    assert config.retries["total_max_attempts"] == 2
    assert list(base.iterdir()) == []


@pytest.mark.parametrize("state", ["COMPLETED", "FAILED", "CANCELLED", "OUTDATED", "REPORT_GENERATION", ""])
def test_inactive_sessions_cannot_query_ecs(runtime, state):
    item, _, calls, _ = runtime
    item["state"]["S"] = state
    assert not json.loads(mcp_server.inspect_ecs_task_control(TASK, CLUSTER))["ok"]
    assert [c[0] for c in calls] == ["dynamodb"]


@pytest.mark.parametrize(
    "missing", ["RCA_EXECUTION_TOKEN", "RCA_SESSION_ID", "RCA_CLAIM_TOKEN", "RCA_ECS_CONTROL_DISCOVERY"]
)
def test_missing_context_never_queries_ecs(runtime, monkeypatch, missing):
    monkeypatch.delenv(missing)
    assert not json.loads(mcp_server.inspect_ecs_task_control(TASK, CLUSTER))["ok"]
    assert runtime[2] == []


def test_invalid_run_token_and_absent_runtime_table_never_query_aws(runtime, monkeypatch):
    monkeypatch.setenv("RCA_EXECUTION_TOKEN", "../another-session")
    assert not json.loads(mcp_server.inspect_ecs_task_control(TASK, CLUSTER))["ok"]
    assert runtime[2] == []
    monkeypatch.setenv("RCA_EXECUTION_TOKEN", runtime[3].name)
    monkeypatch.setattr(mcp_server.settings, "DYNAMODB_TABLE_NAME", "")
    assert not json.loads(mcp_server.inspect_ecs_task_control(TASK, CLUSTER))["ok"]
    assert runtime[2] == []


def test_stale_claim_missing_session_and_missing_alarm_never_query_ecs(runtime):
    item, ddb, calls, _ = runtime
    for response in (
        {},
        {"Item": {**item, "claim_token": {"S": "other-claim"}}},
        {"Item": {**item, "alarm_data": {"S": "{}"}}},
    ):
        calls.clear()
        ddb.get_item.return_value = response
        assert not json.loads(mcp_server.inspect_ecs_task_control(TASK, CLUSTER))["ok"]
        assert [c[0] for c in calls] == ["dynamodb"]


def test_claim_lost_during_read_discards_projection(ecs, task_response, runtime):
    item, ddb, _, _ = runtime
    cancelled = deepcopy(item)
    cancelled["state"]["S"] = "CANCELLED"
    ddb.get_item.side_effect = [{"Item": item}, {"Item": cancelled}]
    with Stubber(ecs) as stub:
        add_success(stub, task_response)
        result = json.loads(mcp_server.inspect_ecs_task_control(TASK, CLUSTER))
    assert result["ok"] is False
    assert "task" not in result


def test_mcp_aws_failure_is_timestamped_and_does_not_change_causal_confirmation(
    ecs, runtime, analysis_artifacts, save_validation, judgment
):
    with Stubber(ecs) as stub:
        stub.add_client_error(
            "describe_tasks", "AccessDeniedException", "private-aws-error", expected_params=TASK_PARAMS
        )
        response = mcp_server.inspect_ecs_task_control(TASK, CLUSTER)
    result = json.loads(response)
    assert not result["ok"]
    assert "DescribeTasks unavailable" in result["error"]
    assert "private-aws-error" not in response
    assert datetime.fromisoformat(result["observed_at"]).tzinfo is not None
    # Missing control is not a new validation gate or an invitation to lower
    # confidence. The unmodified reducer still hands a causal 0.99 to Report.
    save_validation(1, confirmed=[judgment("root", 0.99, evidence=["current blocker ShareLock"])])
    state = replay_analysis(analysis_artifacts).effective_state_view()
    assert state["decision"]["action"] == "REPORT"
    assert state["selected_hypothesis_id"] == "root"
    assert list(runtime[3].iterdir()) == []


def test_mcp_rejects_cross_account_before_creating_ecs_client(runtime):
    result = json.loads(mcp_server.inspect_ecs_task_control(TASK.replace(SCOPE[2], "999999999999"), CLUSTER))
    assert not result["ok"]
    assert [c[0] for c in runtime[2]] == ["dynamodb"]


@pytest.mark.parametrize("profile", list(_PROFILE_CONFIG))
def test_only_production_rca_catalogs_enable_control_discovery(tmp_path, profile):
    home = tmp_path / profile
    home.mkdir()
    config_path = prepare_codex_home(home, profile)
    for path in [config_path, *home.glob("agents/*.toml")]:
        config = tomllib.loads(path.read_text())
        enabled = profile == "analysis-rca" or (profile == "analysis" and path.name == "rca-specialist.toml")
        server = config.get("mcp_servers", {}).get("rca-progress", {})
        assert ("inspect_ecs_task_control" in server.get("enabled_tools", [])) == enabled
        assert (server.get("env", {}).get("RCA_ECS_CONTROL_DISCOVERY") == "1") == enabled
        assert config.get("sandbox_mode") == "read-only"


@pytest.mark.parametrize("role", ["orchestrator", "rca"])
def test_compiled_prompt_collects_control_alongside_cause_before_handoff(role):
    prompt = build_prompt(AlarmContext(alarm_name="offline-control-contract"), role=role)
    for text in (
        "선택 원인의 증거 수집과 함께 제어 메타데이터",
        "`@log`/`@logStream`",
        "`ecs_runtime_identity`",
        "TaskARN과 Cluster ARN",
        "inspect_ecs_task_control(task_arn, cluster_arn)",
        '조회는 `DescribeTasks(include=["TAGS"])`만 사용한다',
        "`task_definition_arn_derived`(ARN-derived)",
        "정의 상태·등록 시각·컨테이너 정의를 추가 조회하지 않는다",
        "강한 validation을 저장하기 전에",
        "추가 가설이나 추가 validation 루프가 아니다",
        "model-eval에서는 제공 관측만 사용",
        "원인 확정을 허용하고 수동 계획",
        "rollback/close **요청**",
        "실제 rollback 완료는 해당 release 로그",
        "`evidence_summary`",
        "최고 신뢰도가 0.9 이상이면 즉시 `REPORT`",
        "전체 validation은 최대 3회",
    ):
        assert text in prompt


def test_compiled_prompt_binds_owner_event_before_resolving_task_identity():
    for role in ("orchestrator", "rca"):
        prompt = build_prompt(AlarmContext(alarm_name="owner-observer-contract"), role=role)
        owner_match = prompt.index("먼저 차단 PID·트랜잭션 시작 시각·runId/application_name")
        owner_stream = prompt.index("소유자 이벤트의 `@log`/`@logStream`")
        inspection = prompt.index("관측된 두 ARN으로 `inspect_ecs_task_control")
        assert owner_match < owner_stream < inspection
        for text in (
            "`db_wait_snapshot`은 서비스 관측자가 다른 DB 세션을 관측해 기록한 이벤트",
            "`activity[]`에 차단 PID가 나타났다는 이유만으로 관측자 서비스 태스크를 차단자에 연결하지 않는다",
            "`maintenance_lock_acquired`",
            "도구가 반환한 태스크 식별자와 소유 run/journal 태그가 소유자 이벤트의 실행과 일치하는지",
            "관측자 태스크로 대체하지 않고 소유권 미확인으로 남긴다",
        ):
            assert text in prompt
