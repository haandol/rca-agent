"""Prepare analysis from a canonical frozen incident before any current recovery eligibility reads."""

from __future__ import annotations

import time
from copy import deepcopy

from headless_codex.ports.dto.models import AlarmContext, parse_alarm
from headless_codex.services.recovery_plan import build_recovery_plan
from headless_codex.services.three_part_analysis import AnalysisPartInterruptedError, AnalysisPartsRun


def frozen_alarm_context(value: dict) -> AlarmContext:
    """Read either engine's persisted alarm without replacing incident time with the new delivery time."""
    if "AlarmName" in value:
        return parse_alarm(value)
    trigger = value.get("trigger") or {}
    if "alarm_name" in value:
        return AlarmContext(
            alarm_name=value["alarm_name"],
            state_reason=value.get("new_state_reason", value.get("state_reason", "")),
            state_change_time=value.get("state_change_time"),
            region=value.get("region", "not provided"),
            metric_name=trigger.get("metric_name", value.get("metric_name")),
            namespace=trigger.get("namespace", value.get("namespace")),
            dimensions=trigger.get("dimensions", value.get("dimensions", {})),
            statistic=trigger.get("statistic", value.get("statistic")),
            period=trigger.get("period", value.get("period")),
            threshold=trigger.get("threshold", value.get("threshold")),
            comparison_operator=trigger.get("comparison_operator", value.get("comparison_operator")),
            alarm_description=value.get("alarm_description"),
        )
    raise ValueError("frozen incident has no supported original alarm identity")


def original_cloudwatch_alarm(value: dict) -> dict:
    """Adapt canonical input for the source reader without borrowing fields from a later delivery."""
    if "AlarmName" in value:
        return deepcopy(value)
    alarm = frozen_alarm_context(value)
    return {
        "AlarmName": alarm.alarm_name,
        "AlarmArn": value.get("alarm_arn"),
        "AlarmDescription": alarm.alarm_description,
        "StateChangeTime": alarm.state_change_time,
        "NewStateReason": alarm.state_reason,
        "Region": alarm.region,
        "Trigger": {
            "MetricName": alarm.metric_name,
            "Namespace": alarm.namespace,
            "Dimensions": [{"name": key, "value": item} for key, item in alarm.dimensions.items()],
        },
    }


def prepare_analysis_parts(
    container, store, *, rca_id: str, alarm_data: dict, claim_token: str, attempt: int, deadline: float, cancel_checker
) -> AnalysisPartsRun:
    """Freeze source once; on redelivery refresh only eligibility against the unchanged original context."""

    def check():
        """Do not start observation or persist results after loss of the parent's common preconditions."""
        if time.monotonic() >= deadline or cancel_checker():
            raise AnalysisPartInterruptedError("analysis preparation interrupted")

    check()
    frozen = store.read_incident(rca_id)
    resumed = frozen is not None
    if frozen is not None:
        incident = frozen["payload"]
        prepared = incident.get("observations", {}).get("recovery_evidence", {})
    else:
        prepared = container.observe_recovery(alarm_data, timeout_seconds=max(0, deadline - time.monotonic()))
        check()
        observations = deepcopy(prepared.get("frozen_observations", {}))
        observations["recovery_evidence"] = deepcopy(prepared)
        incident = {"alarm": deepcopy(alarm_data), "scoping": {}, "observations": observations, "source_artifacts": []}
        frozen = store.freeze_incident(rca_id, claim_token, attempt, incident)
        incident = frozen["payload"]
    eligibility = build_recovery_plan(rca_id, original_cloudwatch_alarm(incident["alarm"]), prepared)
    existing_recovery = store.read_part(rca_id, "recovery")
    if (
        resumed
        and eligibility.get("approval_status") == "READY"
        and not (existing_recovery and existing_recovery.get("payload"))
    ):
        check()
        fresh = container.observe_recovery(
            original_cloudwatch_alarm(incident["alarm"]), timeout_seconds=max(0, deadline - time.monotonic())
        )
        check()
        if fresh.get("context") != prepared.get("context") or fresh.get("verification", {}).get("status") != "VERIFIED":
            eligibility = {
                "approval_status": "UNAVAILABLE",
                "verification": {
                    "valid": False,
                    "reason": "current eligibility no longer matches the immutable incident",
                },
            }
    return AnalysisPartsRun(
        store=store,
        rca_id=rca_id,
        incident=incident,
        claim_token=claim_token,
        attempt=attempt,
        recovery_eligibility=eligibility,
    )
