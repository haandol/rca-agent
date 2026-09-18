"""Project verified recovery evidence without changing stored observations or approval authority."""

import hashlib
import json
from copy import deepcopy


def _digest(value) -> str:
    """Bind omitted repeated evidence to its exact server-owned JSON content."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def recovery_context(scoping, verification: dict) -> dict:
    """Keep complete targets, metrics, distinct facts and source bindings; summarize repeated witnesses only."""
    scoped = scoping.model_dump(mode="json")
    observations = scoped["incident_observations"]
    raw_source = (
        {"incident_ref": deepcopy(verification["incident_ref"])}
        if verification.get("incident_ref")
        else {"status": "server input retained; immutable incident reference not supplied"}
    )
    for phase in ("baseline", "current"):
        section = observations.get(phase, {})
        rows = section.pop("observations", [])
        contracts, manifests = {}, {}
        for row in rows:
            message = row.get("message", {})
            if isinstance(message.get("input_contract"), dict):
                contract = {
                    "sha256": message.get("input_contract_sha256"),
                    "descriptor": message["input_contract"],
                }
                contracts.setdefault(_digest(contract), deepcopy(contract))
            if message.get("event") == "source_manifest":
                manifest = {key: value for key, value in message.items() if key != "observed_at"}
                key = _digest(manifest)
                if key not in manifests:
                    manifests[key] = {
                        "manifest": deepcopy(manifest),
                        "source_binding": {
                            key: deepcopy(row[key])
                            for key in (
                                "source_ref",
                                "log_group",
                                "log_stream",
                                "event_id",
                                "task_definition_arn",
                                "image_digest",
                            )
                            if key in row
                        },
                    }
        section["retained_observation_summary"] = {
            "count": len(rows),
            "sha256": _digest(rows),
            "raw_source": deepcopy(raw_source),
            "distinct_input_contracts": list(contracts.values()),
            "distinct_source_manifests": list(manifests.values()),
        }
    grouped = {}
    witnesses = verification.get("witnesses", [])
    for witness in witnesses:
        binding = {key: value for key, value in witness.items() if key not in {"input_ref", "outcome_ref"}}
        key = _digest(binding)
        pair = {key: witness[key] for key in ("input_ref", "outcome_ref") if key in witness}
        if key not in grouped:
            grouped[key] = {**deepcopy(binding), "count": 0, "first_observed_pair": deepcopy(pair)}
        grouped[key]["count"] += 1
        grouped[key]["last_observed_pair"] = deepcopy(pair)
    projected = {key: deepcopy(value) for key, value in verification.items() if key != "witnesses"}
    projected["witness_summary"] = {
        "count": len(witnesses),
        "sha256": _digest(witnesses),
        "groups": list(grouped.values()),
        "raw_source": {
            **deepcopy(raw_source),
            "verification_field": "server verification.witnesses retained in recovery part",
        },
    }
    projected["full_verification_sha256"] = _digest(verification)
    return {"scoping": scoped, "verification": projected}
