"""Replay the actual ACTIVE→DRAINING false failure; synthetic continuation is labeled separately."""

import copy
import json
import shlex
from pathlib import Path

import pytest
from test_post_action_metrics import Clock

from headless_codex.services.post_action_metrics import ObservationStoppedError, timestamp, utc
from headless_codex.services.service_deployment import poll_deployment, service_settings


@pytest.fixture
def captured():
    """Load unchanged service/task reads from the failed live09 observation, not a reconstructed success."""
    return json.loads((Path(__file__).parent / "fixtures/live09-deployment-transition.json").read_text())


def replay_inputs(captured):
    """Build test-only trusted inputs from recorded identities and the original wait binding."""
    wait = captured["binding"]["request"]
    before = captured["before"]
    normal = next(d for d in before["deployments"] if d["status"] == "PRIMARY")
    fault = next(d for d in before["deployments"] if d["status"] == "ACTIVE")
    guard = {"expected_task_definition": fault["taskDefinition"], "service_settings": service_settings(before)}
    action = {
        "ended_at": captured["binding"]["action_record"],
        "deployment_id": normal["id"],
        "ecs_service_precondition_receipt": {
            "fault_deployment_id": fault["id"],
            "task_arns": [t["taskArn"] for t in captured["prior_task_response"]["tasks"]],
        },
    }
    return wait, guard, action


def run_sequence(captured, snapshots, *, prior_stopped=True, deadline=None, task_mutation=None, no_wait=False):
    """Return recorded first-round task results and injected future service states through real polling logic."""
    wait, guard, action = replay_inputs(captured)
    clock = Clock(timestamp(captured["poll_records"][0]["ended_at"]))
    clock.deadline = timestamp(captured["binding"]["deadline"]) if deadline is None else deadline
    initial_deadline = clock.deadline
    budget = clock.budget(clock.deadline - clock.now)
    reads, receipts = [], []
    service_index = 0
    new_task = captured["poll_records"][3]["stdout"]
    old_task = copy.deepcopy(captured["prior_task_response"])
    if prior_stopped:
        # This is a synthetic subsequent STOPPED receipt, never asserted as part of the live capture.
        for task in old_task["tasks"]:
            task["lastStatus"] = task["desiredStatus"] = "STOPPED"

    def read(command, remaining):
        """Emulate only CLI JSON responses; every deadline, state check and receipt stays production code."""
        nonlocal service_index
        remaining.remaining()
        args = shlex.split(command)
        operation = args[2]
        reads.append(operation)
        if operation == "describe-services":
            selected = snapshots[min(service_index, len(snapshots) - 1)]
            service_index += 1
            payload = (
                {"services": copy.deepcopy(selected)}
                if isinstance(selected, list)
                else {"services": [copy.deepcopy(selected)]}
            )
        elif operation == "list-tasks":
            status = args[args.index("--desired-status") + 1]
            payload = copy.deepcopy(captured["poll_records"][1 if status == "RUNNING" else 2]["stdout"])
        elif operation == "describe-tasks":
            is_old = args[args.index("--tasks") + 1] in action["ecs_service_precondition_receipt"]["task_arns"]
            payload = copy.deepcopy(old_task if is_old else new_task)
            if not is_old and task_mutation:
                task_mutation(payload)
        else:
            raise AssertionError(operation)
        return {"ok": True, "stdout": json.dumps(payload)}

    try:
        result = poll_deployment(wait, guard, action, budget, read, receipts.append, wait_for_convergence=not no_wait)
        return result, receipts, reads, budget, initial_deadline
    except Exception as exc:
        exc.replay_receipts = receipts
        exc.replay_reads = reads
        exc.replay_deadline = budget.deadline
        raise


def completed(captured):
    """Make an explicitly synthetic future stable snapshot for testing after the captured transition."""
    service = copy.deepcopy(captured["after"])
    normal = next(d for d in service["deployments"] if d["status"] == "PRIMARY")
    normal.update(rolloutState="COMPLETED", runningCount=1, pendingCount=0, desiredCount=1)
    service.update(deployments=[normal], runningCount=1, pendingCount=0)
    return service


def test_actual_live09_transition_reobserves_before_synthetic_completion(captured):
    """ACTIVE→DRAINING was the live delta; it must yield PENDING and then fresh stopped-task proof."""
    assert captured["before"]["deployments"][1]["status"] == "ACTIVE"
    assert captured["after"]["deployments"][1]["status"] == "DRAINING"
    stable = completed(captured)
    result, receipts, reads, budget, deadline = run_sequence(
        captured, [captured["before"], captured["after"], stable, stable]
    )
    assert receipts[0]["status"] == "PENDING" and receipts[0]["observation_changed"]
    assert "first_converged_at" not in receipts[0]
    assert result["status"] == "HEALTHY"
    assert len(receipts) == 2 and reads.count("describe-services") == 4
    assert reads.count("describe-tasks") == 3  # new task each round; old STOPPED only on stable round
    assert all(r["binding"]["deadline"] == captured["binding"]["deadline"] for r in receipts)
    assert budget.deadline == deadline
    assert timestamp(result["first_converged_at"]) > timestamp(receipts[0]["observed_at"])


def test_old_deployment_removal_cannot_make_mixed_snapshot_healthy(captured):
    """DRAINING→absent also requires a full fresh observation before assigning the recovery anchor."""
    stable = completed(captured)
    result, receipts, reads, _, _ = run_sequence(captured, [captured["after"], stable, stable, stable])
    assert receipts[0]["status"] == "PENDING" and not receipts[0]["ok"]
    assert result["status"] == "HEALTHY" and reads.count("describe-services") == 4


def test_removal_still_needs_explicit_old_task_stopped_and_original_deadline(captured):
    """The live last old task was STOPPING; disappearance of its deployment does not prove STOPPED."""
    stable = completed(captured)
    assert captured["prior_task_response"]["tasks"][0]["lastStatus"] == "STOPPING"
    deadline = timestamp(captured["poll_records"][0]["ended_at"]) + 11
    with pytest.raises(ObservationStoppedError) as failed:
        run_sequence(captured, [captured["after"], stable, stable, stable], prior_stopped=False, deadline=deadline)
    assert failed.value.replay_deadline == deadline
    assert all(not r["ok"] for r in failed.value.replay_receipts)
    assert all(r["binding"]["deadline"] == utc(deadline) for r in failed.value.replay_receipts)


@pytest.mark.parametrize("change", ["foreign", "failed", "new_primary", "settings", "target", "old_target"])
@pytest.mark.parametrize("which", ["before", "after"])
def test_invalid_snapshot_is_never_treated_as_ordinary_progress(captured, change, which):
    """Both reads independently enforce approved identity/configuration and reject failed/foreign deployments."""
    before, after = copy.deepcopy(captured["before"]), copy.deepcopy(captured["after"])
    service = before if which == "before" else after
    if change == "foreign":
        service["deployments"].append({"id": "foreign", "status": "ACTIVE", "taskDefinition": "foreign"})
    elif change == "failed":
        service["deployments"][0]["rolloutState"] = "FAILED"
    elif change == "new_primary":
        service["deployments"][0]["status"] = "ACTIVE"
        service["deployments"][1]["status"] = "PRIMARY"
    elif change == "settings":
        service["deploymentConfiguration"]["minimumHealthyPercent"] = 99
    elif change == "target":
        service["taskDefinition"] = "foreign"
    else:
        service["deployments"][1]["taskDefinition"] = "foreign"
    with pytest.raises(ValueError):
        run_sequence(captured, [before, after])


def test_foreign_task_is_rejected_even_when_service_is_progressing(captured):
    """A permitted status change cannot discard an observed foreign task and silently try again."""
    with pytest.raises(ValueError, match="foreign task"):
        run_sequence(
            captured,
            [captured["before"], captured["after"]],
            task_mutation=lambda p: p["tasks"][0].update(taskDefinitionArn="foreign"),
        )


def test_metric_continuity_does_not_wait_through_loss_of_convergence(captured):
    """Existing post-convergence checks stay fail-closed instead of shifting the recovery window."""
    with pytest.raises(ValueError, match="no longer converged"):
        run_sequence(captured, [captured["after"], completed(captured)], no_wait=True)


@pytest.mark.parametrize("change", ["rollout_state", "task_counts"])
def test_normal_deployment_completion_is_not_accepted_from_mixed_reads(captured, change):
    """Even unchanged deployment IDs/statuses cannot combine old counters with a later healthy service."""
    stable = completed(captured)
    before = copy.deepcopy(stable)
    if change == "rollout_state":
        before["deployments"][0]["rolloutState"] = "IN_PROGRESS"
    else:
        before.update(runningCount=0, pendingCount=1)
        before["deployments"][0].update(runningCount=0, pendingCount=1)
    result, receipts, reads, _, _ = run_sequence(captured, [before, stable, stable, stable])
    assert receipts[0]["status"] == "PENDING" and receipts[0]["observation_changed"]
    assert "first_converged_at" not in receipts[0]
    assert result["status"] == "HEALTHY" and reads.count("describe-services") == 4
