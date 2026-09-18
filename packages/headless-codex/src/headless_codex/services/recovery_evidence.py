"""Early recovery eligibility from reader-owned observations, never model flags.

Mirrored in Headless; keep validation and negative cases identical. The private
receipt is process-local and intentionally absent from serialized model input.
Persisted recovery parts use their own claim-fenced immutable storage contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime

from headless_codex.services.recovery_observation import build_rollback_context

_HASH = re.compile(r"[a-f0-9]{64}")
_INPUT_PATHS = {
    "adapters/primary/schemas.py",
    "adapters/primary/sensors/sensor_controller.py",
    "ports/dto/sensor.py",
    "services/sensor.py",
    "services/traffic_generator.py",
    "services/input_contract.py",
}
_VITAL_INPUT_PATHS = {
    "adapters/primary/vital_controller.py",
    "ports/dto/vital.py",
    "ports/dto/sensor.py",
    "services/vital.py",
    "services/sensor.py",
    "services/input_contract.py",
    "adapters/secondary/vital_repository/postgresql.py",
    "adapters/secondary/vital_repository/models.py",
    "adapters/secondary/sensor_repository/models.py",
}


def _hash(value) -> str:
    """Hash exact canonical JSON content so changed observations invalidate their receipt."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def _alarm_identity(alarm) -> dict:
    """Bind the receipt to the original alarm coordinates, cutoff and pinned baseline reference."""
    return {
        "arn": alarm.alarm_arn,
        "region": alarm.region,
        "time": str(alarm.state_change_time),
        "description": alarm.alarm_description,
    }


@dataclass(frozen=True)
class _ReaderReceipt:
    observations_hash: str
    alarm_hash: str

    def __deepcopy__(self, memo):
        """Preserve the immutable reader receipt when copying observations without issuing new trust."""
        return self


def seal_observations(observations, alarm) -> None:
    """Called only by the source reader after its normal/current validation."""
    object.__setattr__(
        observations,
        "_recovery_receipt",
        _ReaderReceipt(_hash(observations.model_dump(mode="json")), _hash(_alarm_identity(alarm))),
    )


def has_reader_receipt(observations, alarm) -> bool:
    """Reject deserialized model dictionaries and any mutation of the sealed facts."""
    receipt = getattr(observations, "_recovery_receipt", None)
    return (
        isinstance(receipt, _ReaderReceipt)
        and receipt.observations_hash == _hash(observations.model_dump(mode="json"))
        and receipt.alarm_hash == _hash(_alarm_identity(alarm))
        and alarm.eval_source_metadata is None
    )


def _stamp(value):
    """Require an explicit timezone before comparing input and committed-write evidence windows."""
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("input observation lacks timezone")
    return stamp


def _ref(event) -> str:
    """Reference the observed log group, stream and event without inventing a source identity."""
    return f"cloudwatch-logs://{event['log_group']}/{event['log_stream']}#{event['event_id']}"


def _contract(input_event, manifest_event) -> tuple[str, dict]:
    """Verify actual input receipt and descriptor hashes against installed source, not a claimed flag."""
    message, source = input_event["message"], manifest_event["message"]
    files = source.get("files")
    if (
        source.get("verified") is not True
        or not isinstance(files, dict)
        or not files
        or any(not isinstance(v, str) or not _HASH.fullmatch(v) for v in files.values())
        or hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest() != source.get("fingerprint")
    ):
        raise ValueError("installed source manifest is incomplete or inconsistent")
    descriptor = message.get("input_contract")
    if not isinstance(descriptor, dict):
        raise ValueError("input contract descriptor missing")
    vital = descriptor.get("format") == "vital-event-v1-v2"
    keys = {"format", "timestamp_mapping", "source_files", "runtime"}
    if vital:
        keys |= {"delivery_stage", "event_versions"}
    if set(descriptor) != keys:
        raise ValueError("input contract descriptor missing")
    if vital and (
        descriptor["delivery_stage"] != "measurement_attempt"
        or descriptor["event_versions"] != [1, 2]
        or any(type(version) is not int for version in descriptor["event_versions"])
        or type(message.get("event_schema_version")) is not int
        or message["event_schema_version"] not in (1, 2)
        or type(message.get("count")) is not int
        or message["count"] != 1
    ):
        raise ValueError("actual versioned measurement attempt missing")
    if (
        descriptor["format"] != ("vital-event-v1-v2" if vital else "sensor-service-batch-v1")
        or descriptor["timestamp_mapping"]
        != ("v1.timestamp-or-v2.sampled_at-to-timestamp" if vital else "timestamp-or-server-utc")
        or not isinstance(descriptor["source_files"], dict)
        or set(descriptor["source_files"]) != (_VITAL_INPUT_PATHS if vital else _INPUT_PATHS)
        or any(files.get(path) != digest for path, digest in descriptor["source_files"].items())
        or not isinstance(descriptor["runtime"], dict)
        or set(descriptor["runtime"]) != {"python", "pydantic"}
        or any(
            not isinstance(v, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]*)", v)
            for v in descriptor["runtime"].values()
        )
        or _hash(descriptor) != message.get("input_contract_sha256")
    ):
        raise ValueError("input contract is not bound to the installed input path")
    if (
        message.get("operation") != "ingest"
        or not isinstance(message.get("request_id"), str)
        or not re.fullmatch(r"[a-f0-9]{32}", message["request_id"])
        or type(message.get("count")) is not int
        or message["count"] <= 0
    ):
        raise ValueError("actual normalized input receipt missing")
    return message["input_contract_sha256"], descriptor


def verify_input_compatibility(baseline: dict, current: dict) -> dict:
    """Join normal commit and current failed input to task-scoped installed code."""
    normal_events, current_events = baseline["observations"], current["observations"]
    if any(
        current["log_window"]["coverage"].get(kind) != "complete"
        for kind in ("source_manifest", "input_contract_observed", "db_write_error")
    ):
        raise ValueError("current input/source/error observation coverage incomplete")
    normal_streams = {e["log_stream"] for e in normal_events}
    current_streams = {
        f"{current['log_stream_prefix']}/{baseline['scope']['container_name']}/{t['task_arn'].rsplit('/', 1)[-1]}"
        for t in current["tasks"]
    }
    if not normal_streams or not current_streams:
        raise ValueError("input source tasks missing")
    digests, witnesses = set(), []
    for phase, events, streams, target in (
        ("normal", normal_events, normal_streams, baseline["normal"]),
        ("current", current_events, current_streams, current),
    ):
        for stream in sorted(streams):
            scoped = [e for e in events if e["log_stream"] == stream]
            if any(
                e["log_group"] != baseline["scope"]["log_group"]
                or e["task_definition_arn"] != target["task_definition_arn"]
                or e["image_digest"] != target["image_digest"]
                or not e.get("event_id")
                for e in scoped
            ):
                raise ValueError("input provenance differs from the observed deployment")
            manifests = [e for e in scoped if e["message"].get("event") == "source_manifest"]
            inputs = [e for e in scoped if e["message"].get("event") == "input_contract_observed"]
            outcome_kind = "write_completed" if phase == "normal" else "db_write_error"
            outcomes = [e for e in scoped if e["message"].get("event") == outcome_kind]
            if not manifests or not inputs or not outcomes:
                raise ValueError(f"{phase} input/source/write evidence missing")
            matched = []
            for receipt in inputs:
                for source in manifests:
                    digest, descriptor = _contract(receipt, source)
                    digests.add(digest)
                    if source["timestamp"] > receipt["timestamp"]:
                        raise ValueError("input predates its installed source observation")
                for outcome in outcomes:
                    message = outcome["message"]
                    if message.get("request_id") != receipt["message"]["request_id"]:
                        continue
                    if receipt["timestamp"] > outcome["timestamp"]:
                        raise ValueError("input receipt follows its write outcome")
                    if phase == "normal" and (
                        message.get("completion_semantics") != "committed_rows"
                        or type(message.get("count")) is not int
                        or message["count"] != receipt["message"]["count"]
                        or not _stamp(baseline["metric_observations"]["start"])
                        <= _stamp(message["observed_at"])
                        < _stamp(baseline["metric_observations"]["end"])
                    ):
                        raise ValueError("normal input has no matching committed rows")
                    if phase == "current" and not re.fullmatch(r"[A-Z0-9]{5}", message.get("sqlstate", "")):
                        raise ValueError("current input lacks actual database failure")
                    matched.append(
                        {
                            "phase": phase,
                            "input_ref": _ref(receipt),
                            "outcome_ref": _ref(outcome),
                            "source_refs": [_ref(e) for e in manifests],
                            "task_definition_arn": target["task_definition_arn"],
                            "image_digest": target["image_digest"],
                            "input_contract_sha256": digest,
                            **(
                                {"event_schema_version": receipt["message"]["event_schema_version"]}
                                if descriptor["format"] == "vital-event-v1-v2"
                                else {}
                            ),
                        }
                    )
            if not matched:
                raise ValueError(f"{phase} input does not join a write outcome")
            witnesses.extend(matched)
    if len(digests) != 1:
        raise ValueError("normal and current input contracts differ")
    if descriptor["format"] == "vital-event-v1-v2":
        normal_versions = {w["event_schema_version"] for w in witnesses if w["phase"] == "normal"}
        current_versions = {w["event_schema_version"] for w in witnesses if w["phase"] == "current"}
        if not current_versions <= normal_versions:
            raise ValueError("normal image has no actual matching event-version commit")
    return {
        "status": "VERIFIED",
        "input_contract_sha256": next(iter(digests)),
        "input_format": descriptor["format"],
        "witnesses": witnesses,
    }


def prepare_recovery_evidence(scoping) -> dict:
    """Return immutable-ready observations/context or a bounded unavailable reason.

    This only supplies evidence, not READY: the stage must also validate its full
    plan and persist the frozen incident before publishing approval authority.
    """
    result = {
        "context": None,
        "frozen_observations": {},
        "verification": {"status": "UNAVAILABLE", "reason": "server observation receipt missing"},
    }
    if scoping is None or scoping.raw_alarm is None:
        return result
    observations = scoping.incident_observations
    try:
        if not has_reader_receipt(observations, scoping.raw_alarm):
            return result
        result["frozen_observations"] = deepcopy(observations.model_dump(mode="json"))
        context = build_rollback_context(scoping)
        if context is None:
            raise ValueError("normal/current scope, deployment or settings unavailable")
        verification = verify_input_compatibility(observations.baseline, observations.current)
        result.update(context=context, verification=verification)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        result["verification"] = {"status": "UNAVAILABLE", "reason": str(exc)[:300]}
    return result
