"""Exercise rollback authorization and convergence against controlled real-shaped ECS responses."""

import copy
import json
import shlex
import subprocess
import time

import pytest
from test_post_action_metrics import Clock, response
from test_post_action_metrics import data as original_metric_fixture

from headless_codex import execution_mcp_server as server
from headless_codex.services import execution_workspace as wm
from headless_codex.services.command_gate import evaluate_command
from headless_codex.services.execution_contract import approved_wait, validate_steps
from headless_codex.services.post_action_metrics import (
    ObservationStoppedError,
    bind_request,
    poll_fixed_metrics,
    timestamp,
    utc,
)
from headless_codex.services.runbook_contract import (
    SERVICE_SETTING_DEFAULTS,
    render_step_operation,
    validate_runbook,
)
from headless_codex.services.service_deployment import (
    check_precondition,
    poll_deployment,
    verify_update_response,
)

PREFIX = "arn:aws:ecs:us-east-1:123456789012:"
FAULT = PREFIX + "task-definition/app:2"
NORMAL = PREFIX + "task-definition/app:1"
BAD_DIGEST = "sha256:" + "b" * 64
GOOD_DIGEST = "sha256:" + "a" * 64
OLD_TASK = PREFIX + "task/cluster/old"
NEW_TASK = PREFIX + "task/cluster/new"


@pytest.fixture(name="metric_data")
def metric_fixture():
    return original_metric_fixture.__wrapped__()


@pytest.fixture
def plan():
    scope = {
        "account_id": "123456789012",
        "region": "us-east-1",
        "cluster": PREFIX + "cluster/cluster",
        "service": PREFIX + "service/cluster/app",
        "container_name": "app",
        "desired_count": 1,
    }
    settings = copy.deepcopy(SERVICE_SETTING_DEFAULTS)
    settings.update(launchType="FARGATE", platformVersion="LATEST")
    guard = {
        **scope,
        "expected_task_definition": FAULT,
        "expected_image_digest": BAD_DIGEST,
        "expected_deployment_id": "ecs-svc/fault",
        "service_settings": settings,
    }
    wait = {
        **scope,
        "action_step_id": "rollback",
        "task_definition": NORMAL,
        "image_digest": GOOD_DIGEST,
        "max_wait_seconds": 300,
    }
    baseline = {
        "baseline_ref": {"bucket": "evidence", "key": "normal/run.json", "sha256": "c" * 64},
        "scope": {
            **{k: v for k, v in scope.items() if k not in {"cluster", "service"}},
            "cluster_arn": scope["cluster"],
            "service_arn": scope["service"],
            "service_name": "app",
            "log_group": "/ecs/app",
        },
        "normal": {"task_definition_arn": NORMAL, "image_digest": GOOD_DIGEST},
        "current": {"task_definition_arn": FAULT, "image_digest": BAD_DIGEST, "deployment_id": "ecs-svc/fault"},
        "service_settings": settings,
    }
    metric = copy.deepcopy(original_metric_fixture.__wrapped__()["context"]["playbook"]["execution_steps"][2])
    metric["action"] = "Observe recovered writes"
    del metric["metric_wait"]["action_step_id"]
    metric["metric_wait"]["deployment_step_id"] = "converge"
    return {
        "rollback_context": baseline,
        "execution_steps": [
            {
                "step_id": "rollback",
                "action": "roll back approved image",
                "success_criteria": "approved rollback acknowledged",
                "commands": [
                    f"aws ecs update-service --cluster {scope['cluster']} --service {scope['service']} "
                    f"--task-definition {NORMAL} --region us-east-1"
                ],
                "ecs_service_precondition": guard,
            },
            {
                "step_id": "converge",
                "action": "observe app convergence",
                "success_criteria": "all healthy app tasks and fault stopped",
                "deployment_wait": wait,
            },
            metric,
        ],
    }


class ECS:
    def __init__(self, plan):
        self.guard = plan["execution_steps"][0]["ecs_service_precondition"]
        self.wait = plan["execution_steps"][1]["deployment_wait"]
        self.commands = []
        self.mode = "fault"
        self.foreign = False
        self.old_draining = False
        self.bad_app = False
        self.bad_sidecar = False
        self.config_drift = False
        self.fail_target = False

    def task(self, old=False):
        good = not old
        return {
            "taskArn": OLD_TASK if old else NEW_TASK,
            "clusterArn": self.guard["cluster"],
            "group": "service:app",
            "taskDefinitionArn": FAULT if old else NORMAL,
            "lastStatus": "RUNNING",
            "desiredStatus": "RUNNING",
            "healthStatus": "HEALTHY",
            "containers": [
                {
                    "name": "sidecar",
                    "lastStatus": "RUNNING",
                    "imageDigest": BAD_DIGEST if self.bad_sidecar else GOOD_DIGEST,
                    "healthStatus": "HEALTHY",
                },
                {
                    "name": "app",
                    "lastStatus": "RUNNING",
                    "imageDigest": GOOD_DIGEST if good and not self.bad_app else BAD_DIGEST,
                    "healthStatus": "HEALTHY",
                },
            ],
        }

    def service(self):
        good = self.mode != "fault"
        result = {
            **copy.deepcopy(self.guard["service_settings"]),
            "serviceArn": self.guard["service"],
            "clusterArn": self.guard["cluster"],
            "status": "ACTIVE",
            "desiredCount": 1,
            "runningCount": 1,
            "pendingCount": 0,
            "taskDefinition": NORMAL if good else FAULT,
            "deployments": [
                {
                    "id": "foreign" if self.foreign else "ecs-svc/normal" if good else "ecs-svc/fault",
                    "status": "PRIMARY",
                    "rolloutState": "COMPLETED",
                    "runningCount": 1,
                    "pendingCount": 0,
                    "taskDefinition": NORMAL if good else FAULT,
                }
            ],
        }
        if self.config_drift:
            result["networkConfiguration"] = {"awsvpcConfiguration": {"subnets": ["foreign"]}}
        return result

    def execute(self, text, budget):
        budget.remaining()
        argv = shlex.split(text)
        self.commands.append(argv)
        op = argv[2]
        assert evaluate_command(text).allowed
        if op == "describe-services":
            payload = {"services": [self.service()]}
        elif op == "list-tasks":
            pending = argv[argv.index("--desired-status") + 1] == "PENDING"
            payload = {"taskArns": [] if pending else [OLD_TASK if self.mode == "fault" else NEW_TASK]}
        elif op == "describe-tasks":
            task = argv[argv.index("--tasks") + 1]
            row = self.task(old=task == OLD_TASK)
            if task == OLD_TASK and self.mode != "fault" and not self.old_draining:
                row.update(lastStatus="STOPPED", desiredStatus="STOPPED")
            payload = {"tasks": [row]}
        elif op == "describe-task-definition":
            payload = {
                "taskDefinition": {
                    "taskDefinitionArn": NORMAL,
                    "status": "INACTIVE" if self.fail_target else "ACTIVE",
                    "containerDefinitions": [{"name": "app", "image": "repo@" + GOOD_DIGEST}],
                }
            }
        elif op == "update-service":
            self.mode = "healthy"
            payload = {"service": self.service()}
        else:
            raise AssertionError(argv)
        return {"ok": True, "stdout": json.dumps(payload), "stderr": ""}


def action_receipt(ecs, clock):
    pre = check_precondition(ecs.guard, ecs.wait, clock.budget(), ecs.execute)
    ecs.mode = "healthy"
    dep = verify_update_response({"service": ecs.service()}, ecs.guard, ecs.wait, pre)
    return {"ended_at": utc(clock.now), "deployment_id": dep, "ecs_service_precondition_receipt": pre}


def test_complete_contract_and_rendering_survive_round_trip(plan):
    before = copy.deepcopy(plan)
    validate_runbook(plan["execution_steps"])
    validate_steps(plan)
    from boto3.dynamodb.types import TypeDeserializer

    from headless_codex.services.artifact_watcher import _build_playbook_metadata

    metadata = _build_playbook_metadata(plan)
    decoded = TypeDeserializer().deserialize(metadata["execution_steps"])
    assert decoded[0]["ecs_service_precondition"] == plan["execution_steps"][0]["ecs_service_precondition"]
    assert decoded[1]["deployment_wait"] == plan["execution_steps"][1]["deployment_wait"]
    assert TypeDeserializer().deserialize(metadata["rollback_context"]) == plan["rollback_context"]
    for step, field in zip(
        plan["execution_steps"], ("ecs_service_precondition", "deployment_wait", "metric_wait"), strict=True
    ):
        assert json.dumps(step[field], indent=2) in "\n".join(render_step_operation(step))
    from headless_codex.services.artifact_validation import _render_playbook

    rendered_plan = {**plan, "playbook_id": "pb", "verification_status": "DRAFT"}
    rendered_plan["execution_steps"] = [{**step, "intent": "approved operation"} for step in plan["execution_steps"]]
    report = _render_playbook(rendered_plan, confirmed=True)
    assert json.dumps(plan["rollback_context"], indent=2) in report
    assert json.dumps(plan["execution_steps"][1]["deployment_wait"], indent=2) in report
    assert plan == before


@pytest.mark.parametrize(
    "field", ["expected_task_definition", "expected_image_digest", "expected_deployment_id", "service_settings"]
)
def test_missing_guard_field_rejected(plan, field):
    del plan["execution_steps"][0]["ecs_service_precondition"][field]
    with pytest.raises(ValueError):
        validate_runbook(plan["execution_steps"])
    with pytest.raises(ValueError):
        validate_steps(plan)


@pytest.mark.parametrize(
    "change",
    [
        "strip_guard",
        "extra_command",
        "changed_target",
        "dual_wait",
        "future_action",
        "wait_over_budget",
        "wait_bool",
        "foreign_scope",
    ],
)
def test_unapproved_or_ambiguous_operation_never_validates(plan, change):
    action, wait = plan["execution_steps"][:2]
    if change == "strip_guard":
        del action["ecs_service_precondition"]
    elif change == "extra_command":
        action["commands"][0] += " --force-new-deployment"
    elif change == "changed_target":
        action["commands"][0] = action["commands"][0].replace(NORMAL, FAULT)
    elif change == "dual_wait":
        wait["commands"] = action["commands"]
    elif change == "future_action":
        wait["deployment_wait"]["action_step_id"] = "converge"
    elif change in ("wait_over_budget", "wait_bool"):
        wait["deployment_wait"]["max_wait_seconds"] = 901 if change == "wait_over_budget" else True
    else:
        wait["deployment_wait"]["region"] = "us-west-2"
    with pytest.raises(ValueError):
        validate_steps(plan)


@pytest.mark.parametrize(
    "change", ["missing", "scope", "digest", "definition", "deployment", "ref", "settings", "log_group"]
)
def test_baseline_provenance_must_match_approval(plan, change):
    baseline = plan["rollback_context"]
    if change == "missing":
        del plan["rollback_context"]
    elif change == "scope":
        baseline["scope"]["service_arn"] += "other"
    elif change == "digest":
        baseline["normal"]["image_digest"] = BAD_DIGEST
    elif change == "definition":
        baseline["normal"]["task_definition_arn"] = FAULT
    elif change == "deployment":
        baseline["current"]["deployment_id"] = "other"
    elif change == "settings":
        baseline["service_settings"] = {}
    elif change == "log_group":
        del baseline["scope"]["log_group"]
    else:
        baseline["baseline_ref"]["sha256"] = "unverified"
    with pytest.raises(ValueError):
        validate_steps(plan)


@pytest.mark.parametrize("change", ["already_healthy", "foreign", "config_drift", "fail_target"])
def test_precondition_rejects_actual_drift(plan, change):
    ecs = ECS(plan)
    if change == "already_healthy":
        ecs.mode = "healthy"
    else:
        setattr(ecs, change, True)
    with pytest.raises(ValueError):
        check_precondition(ecs.guard, ecs.wait, Clock().budget(), ecs.execute)
    assert not any(c[2] == "update-service" for c in ecs.commands)


def test_convergence_requires_app_digest_not_sidecar_and_old_fault_stopped(plan):
    ecs, clock = ECS(plan), Clock()
    action = action_receipt(ecs, clock)
    ecs.bad_app = True
    with pytest.raises(ObservationStoppedError):
        poll_deployment(ecs.wait, ecs.guard, action, clock.budget(1), ecs.execute, lambda r: None)
    ecs.bad_app, ecs.bad_sidecar, ecs.old_draining = False, True, True
    with pytest.raises(ObservationStoppedError):
        poll_deployment(ecs.wait, ecs.guard, action, clock.budget(1), ecs.execute, lambda r: None)
    ecs.old_draining = False
    receipt = poll_deployment(ecs.wait, ecs.guard, action, clock.budget(), ecs.execute, lambda r: None)
    assert receipt["status"] == "HEALTHY"
    assert receipt["first_converged_at"] == utc(clock.now)
    assert receipt["task_arns"] == [NEW_TASK]


@pytest.mark.parametrize("change", ["foreign", "config_drift"])
def test_foreign_deployment_or_settings_cannot_converge(plan, change):
    ecs, clock = ECS(plan), Clock()
    action = action_receipt(ecs, clock)
    setattr(ecs, change, True)
    with pytest.raises(ValueError):
        poll_deployment(ecs.wait, ecs.guard, action, clock.budget(), ecs.execute, lambda r: None)


@pytest.fixture
def runtime(plan, monkeypatch, tmp_path):
    monkeypatch.setattr(wm, "_WORKSPACE_ROOT", tmp_path)
    workspace = wm.ExecutionWorkspace.create("exec-1")
    workspace.prepare()
    monkeypatch.setenv(wm.EXECUTION_TOKEN_ENV, workspace.token)
    monkeypatch.setenv(wm.EXECUTION_ID_ENV, workspace.execution_id)
    workspace.write_observation_context(playbook=plan, alarm_data={}, alarm_name="observed-errors")
    monkeypatch.setattr(server, "_observation_control", lambda: time.time() + 1000)
    ecs = ECS(plan)

    def process(argv, budget):
        result = ecs.execute(shlex.join(argv), budget)
        if argv[2] == "update-service":
            # The last live read and its server receipt are present before spawning the write.
            assert ecs.commands[-2][2] == "describe-services"
            assert any(r["type"] == "ecs_service_precondition" for r in workspace.read_records())
        return subprocess.CompletedProcess(argv, 0, result["stdout"], "")

    monkeypatch.setattr(server, "_run_observation_process", process)
    return workspace, ecs


def test_public_tools_enforce_guard_exact_command_and_duplicate_wait_receipts(plan, runtime):
    workspace, ecs = runtime
    command = plan["execution_steps"][0]["commands"][0]
    assert json.loads(server.run_playbook_command("rollback", command))["ok"]
    receipt = json.loads(server.wait_for_service_deployment("converge"))
    assert receipt["ok"] and receipt["status"] == "HEALTHY"
    count = len(ecs.commands)
    assert (
        json.loads(server.wait_for_service_deployment("converge"))["first_converged_at"]
        == receipt["first_converged_at"]
    )
    assert len(ecs.commands) == count
    assert not json.loads(server.run_playbook_command("rollback", command))["ok"]
    assert len([c for c in ecs.commands if c[2] == "update-service"]) == 1
    records = workspace.read_records()
    assert len([r for r in records if r.get("type") == "deployment_wait" and r.get("phase") == "terminal"]) == 1
    assert all(
        r["internal_observation"]
        for r in records
        if r.get("type") == "attempt" and "update-service" not in r["command"]
    )


def test_cancel_after_precondition_prevents_actual_write(plan, runtime, monkeypatch):
    _, ecs = runtime
    old = server._append_record

    def append(record):
        result = old(record)
        if record["type"] == "ecs_service_precondition":
            monkeypatch.setattr(
                server, "_observation_control", lambda: (_ for _ in ()).throw(ObservationStoppedError("fenced"))
            )
        return result

    monkeypatch.setattr(server, "_append_record", append)
    assert not json.loads(server.run_playbook_command("rollback", plan["execution_steps"][0]["commands"][0]))["ok"]
    assert not any(c[2] == "update-service" for c in ecs.commands)


def test_interrupted_deployment_wait_never_restarts(plan, runtime):
    workspace, ecs = runtime
    assert json.loads(server.run_playbook_command("rollback", plan["execution_steps"][0]["commands"][0]))["ok"]
    server._append_record(
        {
            "type": "deployment_wait",
            "phase": "started",
            "step_id": "converge",
            "binding": {"request": ecs.wait, "deadline": "2026-09-15T00:00:00Z"},
        }
    )
    before = len(ecs.commands)
    receipt = json.loads(server.wait_for_service_deployment("converge"))
    assert not receipt["ok"] and "no new deadline" in receipt["error"]
    assert len(ecs.commands) == before
    assert json.loads(server.wait_for_service_deployment("converge"))["binding"] == receipt["binding"]
    assert workspace.read_records()[-1]["phase"] == "terminal"


def deployment_metric_context(plan, metric_data):
    context = metric_data["context"]
    steps = context["playbook"]["execution_steps"]
    steps[1:2] = copy.deepcopy(plan["execution_steps"][:2])
    context["playbook"]["rollback_context"] = plan["rollback_context"]
    metric = steps[3]["metric_wait"]
    del metric["action_step_id"]
    metric["deployment_step_id"] = "converge"
    request = approved_wait(steps[3])
    ecs, clock = ECS(plan), Clock(timestamp("2026-09-10T12:33:00Z"))
    write = action_receipt(ecs, clock)
    from test_post_action_metrics import attempt

    write = attempt(plan["execution_steps"][0]["commands"][0], {"service": ecs.service()}, "rollback", **write)
    binding = {
        "request": plan["execution_steps"][1]["deployment_wait"],
        "action_record": write["ended_at"],
        "deadline": "2026-09-10T12:38:00Z",
    }
    started = {
        "type": "deployment_wait",
        "phase": "started",
        "step_id": "converge",
        "execution_id": "exec-1",
        "observed_at": "2026-09-10T12:33:01Z",
        "binding": binding,
    }
    receipt = {
        "type": "deployment_wait",
        "phase": "terminal",
        "step_id": "converge",
        "execution_id": "exec-1",
        "status": "HEALTHY",
        "ok": True,
        "first_converged_at": "2026-09-10T12:34:00Z",
        "observed_at": "2026-09-10T12:34:00Z",
        "deployment_id": "ecs-svc/normal",
        "binding": binding,
    }
    records = metric_data["records"][1:] + [write, started, receipt]
    return request, records, context


def test_metrics_anchor_first_two_next_minute_bins_and_delayed_arrival(plan, metric_data):
    request, records, context = deployment_metric_context(plan, metric_data)
    bound = bind_request(request, records, context, "exec-1", evaluate_command)
    assert bound["start"] == "2026-09-10T12:35:00+00:00"
    assert bound["end"] == "2026-09-10T12:37:00+00:00"
    bound["request"] = request
    clock = Clock(timestamp("2026-09-10T12:34:00Z"))
    history = []

    def delayed(command, budget):
        budget.remaining()
        if clock.now < timestamp(bound["end"]) + 15:
            return response(metric_data, command, points=[])
        return response(metric_data, command)

    receipt = poll_fixed_metrics(bound, clock.budget(), delayed, history.append)
    assert receipt["status"] == "HEALTHY"
    assert receipt["binding"]["start"] == bound["start"]
    assert clock.now < timestamp(bound["anchor"]["ended_at"]) + 300
    assert any(r.get("phase") == "poll" for r in history)


@pytest.mark.parametrize(
    "change", ["other_execution", "failed", "duplicate", "wrong_target", "missing", "no_timestamp"]
)
def test_metrics_never_anchor_untrusted_convergence(plan, metric_data, change):
    request, records, context = deployment_metric_context(plan, metric_data)
    receipt = records[-1]
    if change == "other_execution":
        receipt["execution_id"] = "other"
    elif change == "failed":
        receipt["status"] = "UNOBSERVABLE"
    elif change == "duplicate":
        records.append(copy.deepcopy(receipt))
    elif change == "wrong_target":
        receipt["binding"] = {"request": {"task_definition": FAULT}}
    elif change == "missing":
        records.pop()
    else:
        del receipt["first_converged_at"]
    with pytest.raises(ValueError):
        bind_request(request, records, context, "exec-1", evaluate_command)


@pytest.mark.parametrize("change", ["service", "cluster", "definition", "desired", "primary", "foreign", "config"])
def test_actual_update_response_scope_is_checked(plan, change):
    ecs, clock = ECS(plan), Clock()
    pre = check_precondition(ecs.guard, ecs.wait, clock.budget(), ecs.execute)
    ecs.mode = "healthy"
    payload = {"service": ecs.service()}
    service = payload["service"]
    if change in {"service", "cluster", "definition"}:
        service[{"service": "serviceArn", "cluster": "clusterArn", "definition": "taskDefinition"}[change]] = "other"
    elif change == "desired":
        service["desiredCount"] = 2
    elif change == "primary":
        service["deployments"][0]["id"] = pre["fault_deployment_id"]
    elif change == "foreign":
        service["deployments"].append({"id": "foreign", "status": "ACTIVE", "taskDefinition": FAULT})
    else:
        service["networkConfiguration"] = {"changed": True}
    with pytest.raises(ValueError):
        verify_update_response(payload, ecs.guard, ecs.wait, pre)


def test_metric_period_checks_current_deployment_and_never_reanchors(plan, metric_data):
    request, records, context = deployment_metric_context(plan, metric_data)
    bound = {**bind_request(request, records, context, "exec-1", evaluate_command), "request": request}
    ecs = ECS(plan)
    clock = Clock(timestamp(bound["anchor"]["ended_at"]))
    action = action_receipt(ecs, clock)
    original_anchor = copy.deepcopy(bound["anchor"])
    seen = []

    def continuity(budget):
        if clock.now > timestamp(bound["anchor"]["ended_at"]) + 20:
            ecs.foreign = True
        try:
            poll_deployment(ecs.wait, ecs.guard, action, budget, ecs.execute, seen.append, wait_for_convergence=False)
        except ValueError as exc:
            raise ObservationStoppedError(str(exc)) from exc

    receipt = poll_fixed_metrics(
        bound, clock.budget(), lambda c, b: response(metric_data, c), seen.append, check_scope=continuity
    )
    assert not receipt["ok"] and receipt["status"] == "UNOBSERVABLE"
    assert "foreign" in receipt["error"]
    assert bound["anchor"] == original_anchor


def test_cancelled_and_short_execution_budget_never_emit_convergence(plan):
    ecs, clock = ECS(plan), Clock()
    action = action_receipt(ecs, clock)
    ecs.old_draining = True
    clock.deadline = clock.now + 2
    rows = []
    with pytest.raises(ObservationStoppedError):
        poll_deployment(ecs.wait, ecs.guard, action, clock.budget(), ecs.execute, rows.append)
    assert all(row["status"] != "HEALTHY" for row in rows)
    assert clock.now <= clock.deadline
    clock.deadline += 100
    clock.cancel_at = clock.now
    before = len(ecs.commands)
    with pytest.raises(ObservationStoppedError):
        poll_deployment(ecs.wait, ecs.guard, action, clock.budget(), ecs.execute, rows.append)
    assert len(ecs.commands) == before


@pytest.mark.parametrize(
    "change",
    ["write_missing", "write_other_execution", "write_response", "started_missing", "start_deadline", "anchor_time"],
)
def test_convergence_receipt_requires_complete_same_execution_provenance(plan, metric_data, change):
    request, records, context = deployment_metric_context(plan, metric_data)
    if change == "write_missing":
        del records[-3]
    elif change == "write_other_execution":
        records[-3]["execution_id"] = "other"
    elif change == "write_response":
        records[-3]["stdout"] = "{}"
    elif change == "started_missing":
        del records[-2]
    elif change == "start_deadline":
        records[-2]["binding"]["deadline"] = "2026-09-10T12:33:02Z"
    else:
        records[-1]["first_converged_at"] = "2026-09-10T12:35:00Z"
    with pytest.raises(ValueError):
        bind_request(request, records, context, "exec-1", evaluate_command)


def test_deployment_requires_final_app_health_and_unique_container_identity(plan):
    from headless_codex.services.service_deployment import task_matches

    ecs = ECS(plan)
    task = ecs.task()
    task["containers"][1]["healthStatus"] = "UNKNOWN"
    assert not task_matches(task, ecs.wait, NORMAL, GOOD_DIGEST)
    task["containers"].append(copy.deepcopy(task["containers"][1]))
    with pytest.raises(ValueError, match="duplicate container"):
        task_matches(task, ecs.wait, NORMAL, GOOD_DIGEST)


def test_generic_cli_wait_is_not_blanket_allowed_by_generation(plan):
    step = copy.deepcopy(plan["execution_steps"][0])
    del step["ecs_service_precondition"]
    step["commands"] = ["aws ecs wait services-stable --cluster c --services s --region us-east-1"]
    with pytest.raises(ValueError, match="known AWS service operation"):
        validate_runbook([step])


def test_model_cannot_strip_guard_and_wait_from_reader_owned_rollback(plan):
    plan["execution_steps"] = plan["execution_steps"][:1]
    del plan["execution_steps"][0]["ecs_service_precondition"]
    with pytest.raises(ValueError, match="precondition cannot be stripped"):
        validate_steps(plan)
    # The same generic CLI plan without the new operation's context stays compatible.
    del plan["rollback_context"]
    assert validate_steps(plan)


def test_model_cannot_change_both_command_and_wait_target_against_reader_context(plan):
    target = PREFIX + "task-definition/app:99"
    plan["execution_steps"][0]["commands"][0] = plan["execution_steps"][0]["commands"][0].replace(NORMAL, target)
    plan["execution_steps"][1]["deployment_wait"]["task_definition"] = target
    validate_runbook(plan["execution_steps"])
    with pytest.raises(ValueError, match="reader-verified normal"):
        validate_steps(plan)


def test_public_rollback_convergence_and_metric_tools_share_immutable_anchor(plan, runtime, metric_data, monkeypatch):
    from headless_codex.services.post_action_metrics import ObservationBudget

    workspace, ecs = runtime
    clock = Clock(timestamp("2026-09-10T12:34:00Z"))
    context = metric_data["context"]
    precheck = copy.deepcopy(context["playbook"]["execution_steps"][0])
    metric_step = copy.deepcopy(context["playbook"]["execution_steps"][2])
    del metric_step["metric_wait"]["action_step_id"]
    metric_step["metric_wait"]["deployment_step_id"] = "converge"
    plan["execution_steps"] = [precheck, *plan["execution_steps"][:2], metric_step]
    workspace.write_observation_context(playbook=plan, alarm_data={}, alarm_name=context["alarm_name"])
    monkeypatch.setattr(server, "_now_iso", lambda: utc(clock.now))
    monkeypatch.setattr(server.time, "time", clock.time)
    monkeypatch.setattr(server, "_observation_control", clock.control)
    monkeypatch.setattr(
        server,
        "ObservationBudget",
        lambda deadline, control: ObservationBudget(
            deadline, control, clock=clock.time, monotonic=clock.time, wait=clock.wait
        ),
    )
    original_process = server._run_observation_process

    def process(argv, budget):
        if argv[1] == "cloudwatch":
            result = response(metric_data, shlex.join(argv))
            return subprocess.CompletedProcess(argv, 0, result["stdout"], "")
        return original_process(argv, budget)

    monkeypatch.setattr(server, "_run_observation_process", process)
    for record in metric_data["records"][1:]:
        assert server._append_record(record)
    assert json.loads(server.run_playbook_command("rollback", plan["execution_steps"][1]["commands"][0]))["ok"]
    convergence = json.loads(server.wait_for_service_deployment("converge"))
    assert convergence["ok"]
    assert convergence["first_converged_at"] == "2026-09-10T12:34:00+00:00"
    result = json.loads(server.wait_for_post_action_metrics("verify", **metric_step["metric_wait"]))
    assert result["ok"] and result["status"] == "HEALTHY", result
    assert result["binding"]["start"] == "2026-09-10T12:35:00+00:00"
    assert result["binding"]["end"] == "2026-09-10T12:37:00+00:00"
    assert any(r["type"] == "deployment_continuity" for r in workspace.read_records())
    observed_count = len(ecs.commands)
    duplicate = json.loads(server.wait_for_post_action_metrics("verify", **metric_step["metric_wait"]))
    assert duplicate["binding"] == result["binding"]
    assert len(ecs.commands) == observed_count


def accounting_context(plan):
    """Attach the exact optional normal-image descriptor produced by the analysis reader."""
    context = plan["rollback_context"]
    descriptor = {
        "namespace": "Sensor",
        "dimensions": {"ServiceName": "app"},
        "attempts_metric": "attempts",
        "failures_metric": "failures",
        "operation_kind": "write",
        "accounting": "completed",
        "source_ref": "cloudwatch-logs:///ecs/app/ecs/app/normal-task#producer-event",
        **context["normal"],
    }
    context["write_accounting"] = descriptor
    return descriptor


def test_optional_normal_descriptor_and_legacy_context_validate(plan):
    """Both the original five-key context and the optional six-key shape are accepted."""
    from headless_codex.services.service_deployment import validate_rollback_context

    guard, wait = plan["execution_steps"][0]["ecs_service_precondition"], plan["execution_steps"][1]["deployment_wait"]
    validate_rollback_context(plan, guard, wait)
    accounting_context(plan)
    validate_rollback_context(plan, guard, wait)


@pytest.mark.parametrize(
    "field,value",
    [
        ("image_digest", BAD_DIGEST),
        ("task_definition_arn", FAULT),
        ("source_ref", "cloudwatch-logs:///foreign/ecs/app/normal-task#id"),
        ("source_ref", "cloudwatch-logs:///ecs/app/ecs/other/normal-task#id"),
        ("source_ref", "cloudwatch-logs:///ecs/app/ecs/app/normal-task"),
        ("dimensions", {"ServiceName": "app", "Unknown": "x"}),
        ("accounting", "started"),
        ("operation_kind", "read"),
        ("made_up", "yes"),
    ],
)
def test_optional_descriptor_rejects_forged_provenance(plan, field, value):
    """Fault-image, foreign-source and unknown-shape declarations cannot pass approval validation."""
    from headless_codex.services.service_deployment import validate_rollback_context

    accounting_context(plan)[field] = value
    with pytest.raises(ValueError, match="write_accounting"):
        validate_rollback_context(
            plan, plan["execution_steps"][0]["ecs_service_precondition"], plan["execution_steps"][1]["deployment_wait"]
        )


def test_completed_accounting_requires_known_context_and_restored_image(plan):
    """Only the fixed server-context pointer on the approved normal deployment proves semantics."""
    from headless_codex.services.post_action_metrics import _completed_accounting

    descriptor = accounting_context(plan)
    request = {
        "deployment_step_id": "converge",
        "failure_alarm_name": "observed-errors",
        "max_wait_seconds": 300,
        "region": "us-east-1",
        "metrics": {
            role: {"namespace": "Sensor", "metric_name": role, "dimensions": {"ServiceName": "app"}}
            for role in ("attempts", "failures")
        },
        "completed_work_evidence": {
            "record_index": "approved_context",
            "json_pointer": "/playbook/rollback_context/write_accounting",
        },
    }
    plan["execution_steps"][2]["metric_wait"] = request
    validate_runbook(plan["execution_steps"])
    validate_steps(plan)
    context = {"playbook": plan, "model_authored": descriptor}
    bound = _completed_accounting(request, [], context, "exec", [], evaluate_command)
    assert bound["descriptor"] == descriptor
    request["completed_work_evidence"]["json_pointer"] = "/rollback_context/write_accounting"
    with pytest.raises(ValueError, match="reader-owned"):
        validate_steps(plan)
    with pytest.raises(ValueError, match="reader-owned"):
        validate_runbook(plan["execution_steps"])
    with pytest.raises(ValueError, match="reader-owned"):
        _completed_accounting(request, [], context, "exec", [], evaluate_command)
    request["completed_work_evidence"]["json_pointer"] = "/playbook/rollback_context/write_accounting"
    plan["execution_steps"][1]["deployment_wait"]["image_digest"] = BAD_DIGEST
    with pytest.raises(ValueError, match="restored deployment"):
        _completed_accounting(request, [], context, "exec", [], evaluate_command)
    plan["execution_steps"][1]["deployment_wait"]["image_digest"] = GOOD_DIGEST
    del plan["rollback_context"]["write_accounting"]
    with pytest.raises(ValueError, match="descriptor shape"):
        _completed_accounting(request, [], context, "exec", [], evaluate_command)


def test_normal_accounting_metric_service_name_is_not_ecs_service_name(plan):
    """An approved application's ServiceName dimension may differ from its ECS resource name."""
    from headless_codex.services.service_deployment import validate_rollback_context

    descriptor = accounting_context(plan)
    descriptor["dimensions"] = {"ServiceName": "healthcare-sensor-app"}
    assert plan["rollback_context"]["scope"]["service_name"] != "healthcare-sensor-app"
    validate_rollback_context(
        plan, plan["execution_steps"][0]["ecs_service_precondition"], plan["execution_steps"][1]["deployment_wait"]
    )


@pytest.mark.parametrize(
    "dimensions", [{}, {"ServiceName": ""}, {"ServiceName": None}, {"ServiceName": "app", "Extra": "unknown"}, []]
)
def test_normal_accounting_requires_one_nonempty_metric_service_dimension(plan, dimensions):
    """Separating the two service names does not permit missing or unknown dimensions."""
    from headless_codex.services.service_deployment import validate_write_accounting

    accounting_context(plan)["dimensions"] = dimensions
    with pytest.raises(ValueError, match="write_accounting"):
        validate_write_accounting(plan["rollback_context"])


def test_900_second_wait_allows_later_convergence_without_changing_anchor(plan):
    """A deployment taking more than the old 300 seconds can converge inside the approved 900."""
    plan["execution_steps"][1]["deployment_wait"]["max_wait_seconds"] = 900
    validate_steps(plan)
    ecs, clock = ECS(plan), Clock()
    action = action_receipt(ecs, clock)
    start = clock.now
    ecs.bad_app = True

    def later_convergence(command, budget):
        if clock.now - start >= 400:
            ecs.bad_app = False
        return ecs.execute(command, budget)

    receipt = poll_deployment(ecs.wait, ecs.guard, action, clock.budget(900), later_convergence, lambda row: None)
    assert receipt["status"] == "HEALTHY"
    assert start + 300 < clock.now < start + 900
    assert receipt["first_converged_at"] == utc(clock.now)


def test_900_second_metric_wait_keeps_original_two_bins_during_late_arrival(plan, metric_data):
    """Waiting longer does not move the next-minute anchor or replace the fixed recovery bins."""
    request, records, context = deployment_metric_context(plan, metric_data)
    request["max_wait_seconds"] = 900
    context["playbook"]["execution_steps"][3]["metric_wait"]["max_wait_seconds"] = 900
    bound = bind_request(request, records, context, "exec-1", evaluate_command)
    bound["request"] = request
    clock = Clock(timestamp("2026-09-10T12:34:00Z"))
    start = clock.now

    def delayed(command, budget):
        budget.remaining()
        return response(metric_data, command, points=[]) if clock.now < start + 400 else response(metric_data, command)

    receipt = poll_fixed_metrics(bound, clock.budget(900), delayed, lambda row: None)
    assert receipt["status"] == "HEALTHY"
    assert start + 300 < clock.now < start + 900
    assert receipt["binding"]["start"] == "2026-09-10T12:35:00+00:00"
    assert receipt["binding"]["end"] == "2026-09-10T12:37:00+00:00"


@pytest.mark.parametrize("strategy", [None, []])
def test_capacity_absence_representation_does_not_reject_same_service(plan, strategy):
    """A null/empty capacity strategy describes the same approved launch-type service."""
    from headless_codex.services.service_deployment import service_settings

    ecs = ECS(plan)
    original = ecs.service

    def service_with_absence():
        value = original()
        value["capacityProviderStrategy"] = strategy
        return value

    ecs.service = service_with_absence
    assert service_settings(ecs.service())["capacityProviderStrategy"] == []
    check_precondition(ecs.guard, ecs.wait, Clock().budget(), ecs.execute)
    value = original()
    value.pop("capacityProviderStrategy", None)
    assert service_settings(value)["capacityProviderStrategy"] == []


@pytest.mark.parametrize("change", [None, "reset", "threshold_type", "threshold_value", "missing"])
def test_new_mutable_circuit_breaker_fields_remain_approval_preconditions(plan, change):
    """Same wire state is accepted, but mutable fields cannot be dropped to make a mismatching plan pass."""
    configuration = {
        "enable": True,
        "rollback": True,
        "resetOnHealthyTask": True,
        "thresholdConfiguration": {"type": "BOUNDED_PERCENT", "value": 50},
    }
    plan["execution_steps"][0]["ecs_service_precondition"]["service_settings"]["deploymentConfiguration"] = {
        "deploymentCircuitBreaker": configuration
    }
    ecs = ECS(plan)
    original = ecs.service

    def current():
        value = original()
        breaker = value["deploymentConfiguration"]["deploymentCircuitBreaker"]
        if change == "reset":
            breaker["resetOnHealthyTask"] = False
        elif change == "threshold_type":
            breaker["thresholdConfiguration"]["type"] = "ABSOLUTE"
        elif change == "threshold_value":
            breaker["thresholdConfiguration"]["value"] = 51
        elif change == "missing":
            del breaker["thresholdConfiguration"]
        return value

    ecs.service = current
    if change is None:
        check_precondition(ecs.guard, ecs.wait, Clock().budget(), ecs.execute)
    else:
        with pytest.raises(ValueError):
            check_precondition(ecs.guard, ecs.wait, Clock().budget(), ecs.execute)


@pytest.mark.parametrize("value", [False, 0, "", {}, [{"capacityProvider": "FARGATE", "weight": 1}]])
def test_capacity_strategy_nonabsence_values_are_not_normalized_away(value):
    """Only missing/null is an absence representation; malformed or substantive values stay comparable."""
    from headless_codex.services.service_deployment import service_settings

    assert service_settings({"capacityProviderStrategy": value})["capacityProviderStrategy"] is value
