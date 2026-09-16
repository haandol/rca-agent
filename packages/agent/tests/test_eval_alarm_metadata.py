"""Provided alarm metadata survives model-eval without changing session identity."""

import json

import pytest

from rca_agent import eval_adapter
from rca_agent.adapters.secondary.session.dynamodb_session_store import build_idempotency_key
from rca_agent.ports.dto.models import AlarmPayload

SOURCE_TIME = "2026-09-10T00:06:00Z"
RUN_TIME = "2026-09-10T01:00:00.000001+0000"
METADATA = {
    "stateChangeTime": SOURCE_TIME,
    "region": "ap-northeast-2",
    "namespace": "Example/Healthcare",
    "dimensions": {"Service": "healthcare", "Instance": "db-example"},
    "statistic": "Sum",
    "period": 60,
    "threshold": 0,
    "comparisonOperator": "GreaterThanThreshold",
    "evaluationPeriods": 2,
    "datapointsToAlarm": 1,
    "treatMissingData": "notBreaching",
    "arn": "arn:aws:cloudwatch:ap-northeast-2:123456789012:alarm:A",
}
TRIGGER_FIELDS = {
    "namespace": "Namespace",
    "statistic": "Statistic",
    "period": "Period",
    "threshold": "Threshold",
    "comparisonOperator": "ComparisonOperator",
    "evaluationPeriods": "EvaluationPeriods",
    "datapointsToAlarm": "DatapointsToAlarm",
    "treatMissingData": "TreatMissingData",
}


def scenario(**metadata):
    """Build a minimal eval input with caller-supplied alarm metadata."""
    return {
        "id": "metadata-boundary",
        "executionModes": ["model-eval"],
        "alarm": {"name": "A", "metric": "M", "stateReason": "reason", **metadata},
        "observations": [
            {
                "id": "obs-01",
                "source": "local-log",
                "summary": '{"window":{"start":"2026-09-10T00:03:00Z","end":"2026-09-10T00:06:00Z"}}',
            }
        ],
        "expectation": {"privateAnswer": "must-not-reach-model"},
    }


@pytest.mark.parametrize("source,target", TRIGGER_FIELDS.items())
def test_optional_trigger_fields_are_forwarded_independently(source, target):
    """Test optional trigger fields are forwarded independently."""
    envelope = eval_adapter._alarm_envelope(scenario(**{source: METADATA[source]}), state_change_time=RUN_TIME)
    assert envelope["Trigger"] == {"MetricName": "M", target: METADATA[source]}
    metadata_text = envelope["NewStateReason"].split("Source alarm metadata (provided values):\n")[1].split("\n\n")[0]
    assert json.loads(metadata_text) == {source: METADATA[source]}


def test_complete_envelope_preserves_source_metadata_and_fresh_identity():
    """Test complete envelope preserves source metadata and fresh identity."""
    supplied = scenario(**METADATA)
    envelope = eval_adapter._alarm_envelope(supplied, state_change_time=RUN_TIME)
    assert envelope["Region"] == METADATA["region"]
    assert envelope["AlarmArn"] == METADATA["arn"]
    assert envelope["Trigger"]["Dimensions"] == [
        {"name": name, "value": value} for name, value in METADATA["dimensions"].items()
    ]
    for source, target in TRIGGER_FIELDS.items():
        assert envelope["Trigger"][target] == METADATA[source]
    assert envelope["StateChangeTime"] == RUN_TIME
    assert json.dumps(METADATA, ensure_ascii=False) in envelope["NewStateReason"]
    assert supplied["observations"][0]["summary"] in envelope["NewStateReason"]
    assert "must-not-reach-model" not in envelope["NewStateReason"]
    parsed = AlarmPayload.from_cloudwatch_sns(envelope)
    assert parsed.region == "ap-northeast-2"
    assert parsed.trigger.threshold == 0
    assert parsed.trigger.period == 60
    assert parsed.trigger.statistic == "Sum"
    second = eval_adapter._alarm_envelope(supplied, state_change_time="2026-09-10T01:00:00.000002+0000")
    assert build_idempotency_key(parsed) != build_idempotency_key(AlarmPayload.from_cloudwatch_sns(second))
    assert supplied["alarm"]["stateChangeTime"] == SOURCE_TIME


def test_region_without_arn_is_visible_without_fabricating_an_arn():
    """Test region without arn is visible without fabricating an arn."""
    envelope = eval_adapter._alarm_envelope(scenario(region="ap-northeast-2"), state_change_time=RUN_TIME)
    assert envelope["Region"] == "ap-northeast-2"
    assert "AlarmArn" not in envelope
    assert AlarmPayload.from_cloudwatch_sns(envelope).region == "ap-northeast-2"
    assert '"region": "ap-northeast-2"' in AlarmPayload.from_cloudwatch_sns(envelope).new_state_reason


def test_absent_and_null_metadata_are_not_filled_in_the_envelope():
    """Test absent and null metadata are not filled in the envelope."""
    for supplied in ({}, {name: None for name in METADATA}):
        envelope = eval_adapter._alarm_envelope(scenario(**supplied), state_change_time=RUN_TIME)
        assert envelope["Trigger"] == {"MetricName": "M"}
        assert "Region" not in envelope
        assert "AlarmArn" not in envelope
        assert "Source alarm metadata" not in envelope["NewStateReason"]


def test_empty_dimensions_and_missing_datapoints_are_not_reinterpreted():
    """Test empty dimensions and missing datapoints are not reinterpreted."""
    envelope = eval_adapter._alarm_envelope(scenario(dimensions={}, evaluationPeriods=2), state_change_time=RUN_TIME)
    assert envelope["Trigger"]["Dimensions"] == []
    assert envelope["Trigger"]["EvaluationPeriods"] == 2
    assert "DatapointsToAlarm" not in envelope["Trigger"]


def test_catalog_projection_is_the_only_evidence_sent_to_the_shared_pipeline():
    """Test catalog projection is the only evidence sent to the shared pipeline."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    for path in sorted((root / "tests/scenarios").glob("*.json")):
        supplied = json.loads(path.read_text())
        envelope = eval_adapter._alarm_envelope(supplied, state_change_time=RUN_TIME)
        evidence = eval_adapter.build_precollected_evidence(supplied["observations"])
        text = envelope["NewStateReason"] + evidence
        assert supplied["provenance"]["source"] not in text
        assert supplied["provenance"]["operatorMapping"] not in text
        assert supplied["id"] not in text
        for marker in ("case=", "phase=", "phases[", "restored_write", "release_events", "rollback_complete"):
            assert marker not in text
        for observation in supplied["observations"]:
            assert observation["summary"] in envelope["NewStateReason"]
            assert observation["summary"] in evidence


def render_details(supplied):
    """Render the shared scoping prompt from the real eval envelope and parser."""
    from rca_agent.services.scoping import _build_user_prompt

    envelope = eval_adapter._alarm_envelope(supplied, state_change_time=RUN_TIME)
    alarm = AlarmPayload.from_cloudwatch_sns(envelope)
    return alarm, [_build_user_prompt(alarm, [])]


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_missing_metadata_is_unknown_in_actual_prompt(missing, monkeypatch):
    """Missing fields never become runtime coordinates, defaults, or session times."""
    monkeypatch.setenv("AWS_REGION", "eu-west-3")
    supplied = {key: missing for key in METADATA if key != "dimensions"}
    alarm, prompts = render_details(scenario(**supplied))
    assert alarm.region == "not provided"
    assert alarm.trigger.statistic is None
    assert alarm.trigger.period is None
    assert alarm.alarm_arn is None
    for prompt in prompts:
        for line in (
            "- **Region**: not provided",
            "- **State Change Time**: not provided",
            "- **Metric**: not provided/M",
            "- **Dimensions**: not provided",
            "- **Statistic**: not provided",
            "- **Period**: not provided",
            "- **Threshold**: not provided (not provided)",
        ):
            assert line in prompt.splitlines()
        for invented in ("us-east-1", "eu-west-3", "Average", "300s", RUN_TIME, "arn:aws:"):
            assert invented not in prompt


def test_all_provided_details_and_zero_reach_actual_prompt():
    """Source time, region without ARN, empty dimensions, and numeric zero survive."""
    metadata = {**METADATA, "period": 0, "evaluationPeriods": 0, "datapointsToAlarm": 0, "dimensions": {}}
    metadata.pop("arn")
    supplied = scenario(**metadata)
    original = json.dumps(supplied, sort_keys=True)
    alarm, prompts = render_details(supplied)
    assert alarm.alarm_arn is None
    for prompt in prompts:
        for line in (
            f"- **State Change Time**: {SOURCE_TIME}",
            "- **Region**: ap-northeast-2",
            "- **Metric**: Example/Healthcare/M",
            "- **Dimensions**: {}",
            "- **Statistic**: Sum",
            "- **Period**: 0s",
            "- **Threshold**: 0 (GreaterThanThreshold)",
        ):
            assert line in prompt.splitlines()
        assert json.dumps(metadata, ensure_ascii=False) in prompt
        assert RUN_TIME not in prompt
        assert "arn:aws:" not in prompt
    assert json.dumps(supplied, sort_keys=True) == original


def test_catalog_prompts_preserve_supplied_metadata_and_mark_only_missing_fields_unknown(monkeypatch):
    """Keep observed AWS metadata while missing local fields stay unknown, never runtime defaults."""
    from pathlib import Path

    monkeypatch.setenv("AWS_REGION", "eu-west-3")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-3")
    paths = sorted((Path(__file__).resolve().parents[3] / "tests/scenarios").glob("*.json"))
    assert paths, "the active catalog must provide at least one metadata contract case"
    for path in paths:
        supplied = json.loads(path.read_text())
        original = json.dumps(supplied, sort_keys=True)
        source = supplied["alarm"]
        alarm, prompts = render_details(supplied)
        metadata = {key: source[key] for key in METADATA if key in source and source[key] is not None}
        assert alarm.eval_source_metadata == {**metadata, "metric": source.get("metric")}
        assert alarm.alarm_name == source["name"]
        assert alarm.alarm_description == source.get("description")
        assert alarm.alarm_arn == source.get("arn")

        rendered = {}
        for key in (*METADATA, "metric"):
            value = source.get(key)
            missing = value is None or (isinstance(value, str) and not value.strip())
            rendered[key] = "not provided" if missing else str(value)
            if not missing and key == "dimensions":
                rendered[key] = json.dumps(value, ensure_ascii=False)
            elif not missing and key == "period":
                rendered[key] = f"{value}s"
        assert alarm.region == rendered["region"]
        assert alarm.trigger is not None
        assert alarm.trigger.metric_name == source["metric"]
        assert alarm.trigger.namespace == source["namespace"]
        assert alarm.trigger.dimensions == source.get("dimensions", {})
        for key, attribute in (
            ("statistic", "statistic"),
            ("period", "period"),
            ("threshold", "threshold"),
            ("comparisonOperator", "comparison_operator"),
        ):
            assert getattr(alarm.trigger, attribute) == source.get(key)

        for prompt in prompts:
            for line in (
                f"- **Region**: {rendered['region']}",
                f"- **State Change Time**: {rendered['stateChangeTime']}",
                f"- **Metric**: {rendered['namespace']}/{rendered['metric']}",
                f"- **Dimensions**: {rendered['dimensions']}",
                f"- **Statistic**: {rendered['statistic']}",
                f"- **Period**: {rendered['period']}",
                f"- **Threshold**: {rendered['threshold']} ({rendered['comparisonOperator']})",
            ):
                assert line in prompt.splitlines(), path.name
            assert json.dumps(metadata, ensure_ascii=False) in prompt
            assert "eu-west-3" not in prompt
            assert RUN_TIME not in prompt
        assert json.dumps(supplied, sort_keys=True) == original


def test_production_defaults_remain_compatible():
    """Non-eval parsing and scoping retain existing ARN, time, and trigger defaults."""
    from rca_agent.services.scoping import _build_user_prompt

    alarm = AlarmPayload.from_cloudwatch_sns(
        {
            "AlarmName": "A",
            "StateChangeTime": SOURCE_TIME,
            "Trigger": {"MetricName": "M"},
        }
    )
    assert alarm.eval_source_metadata is None
    assert alarm.region == "us-east-1"
    prompt = _build_user_prompt(alarm, [])
    assert "- **Region**: us-east-1" in prompt.splitlines()
    assert "- **Statistic**: Average" in prompt.splitlines()
    assert "- **Period**: 300s" in prompt.splitlines()
    assert "2026-09-10 00:06:00+00:00" in prompt
    parsed = AlarmPayload.from_cloudwatch_sns({"AlarmArn": METADATA["arn"]})
    assert parsed.region == "ap-northeast-2"


def test_non_eval_envelope_does_not_enable_source_overrides():
    """Only model-eval requests opt into the source-only metadata view."""
    supplied = scenario()
    supplied["executionModes"] = ["deployed-e2e"]
    envelope = eval_adapter._alarm_envelope(supplied, state_change_time=RUN_TIME)
    assert "EvalSourceMetadata" not in envelope


@pytest.mark.parametrize(
    "source,line",
    [
        ("stateChangeTime", f"- **State Change Time**: {SOURCE_TIME}"),
        ("region", "- **Region**: ap-northeast-2"),
        ("namespace", "- **Metric**: Example/Healthcare/M"),
        ("dimensions", '- **Dimensions**: {"Service": "healthcare", "Instance": "db-example"}'),
        ("statistic", "- **Statistic**: Sum"),
        ("period", "- **Period**: 60s"),
        ("threshold", "- **Threshold**: 0 (not provided)"),
        ("comparisonOperator", "- **Threshold**: not provided (GreaterThanThreshold)"),
    ],
)
def test_individually_provided_fields_reach_actual_prompt(source, line):
    """A single provided field survives without enabling defaults for other fields."""
    _, prompts = render_details(scenario(**{source: METADATA[source]}))
    for prompt in prompts:
        assert line in prompt.splitlines()
        if source != "region":
            assert "- **Region**: not provided" in prompt.splitlines()
        assert "us-east-1" not in prompt
        assert "arn:aws:" not in prompt
        assert RUN_TIME not in prompt


def test_completely_omitted_metadata_reaches_actual_prompt_as_unknown():
    """Omitted keys follow the same unknown rendering as explicit nulls."""
    _, prompts = render_details(scenario())
    assert "- **Region**: not provided" in prompts[0].splitlines()
    assert "- **Period**: not provided" in prompts[0].splitlines()
    assert "- **State Change Time**: not provided" in prompts[0].splitlines()


def test_missing_eval_conditions_survive_notification_context():
    """Absent optional conditions remain serializable at the shared pipeline's output."""
    from rca_agent.services.notification import _build_alarm_context

    alarm, _ = render_details(scenario())
    context = _build_alarm_context(alarm)
    assert context.statistic is None
    assert context.period is None
    assert context.region == "not provided"
