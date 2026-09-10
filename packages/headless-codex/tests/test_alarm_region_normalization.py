"""Offline region normalization regressions; no AWS or model calls."""

import json
from copy import deepcopy

import pytest

from headless_codex import eval_adapter
from headless_codex.ports.dto.models import parse_alarm
from headless_codex.services.pipeline import parse_sns_envelope
from headless_codex.services.prompt_builder import build_prompt

# Region and AlarmArn reproduced from the 2026-09-10 custlock source alarm.
SNS_ALARM = {
    "AlarmName": "RcaAgentDev-Healthcare-VitalIngestFailures",
    "Region": "US East (N. Virginia)",
    "AlarmArn": "arn:aws:cloudwatch:us-east-1:395271362395:alarm:RcaAgentDev-Healthcare-VitalIngestFailures",
    "AlarmDescription": "Source description stays uninterpreted.",
    "FutureField": {"keep": [1, None, {"Region": "source label"}]},
    "Trigger": {
        "MetricName": "VitalIngestFailures",
        "Namespace": "Healthcare/Sensor",
        "Dimensions": [{"name": "ServiceName", "value": "healthcare-sensor-app"}],
        "EvaluationPeriods": 2,
    },
}

SUPPORTED_REGIONS = [
    ("aws", "us-east-1"),
    ("aws", "ap-northeast-2"),
    ("aws", "eu-central-2"),
    ("aws", "mx-central-1"),
    ("aws-us-gov", "us-gov-east-1"),
    ("aws-us-gov", "us-gov-west-1"),
    ("aws-cn", "cn-north-1"),
    ("aws-cn", "cn-northwest-1"),
]

INVALID_ARNS = [
    None,
    "",
    123,
    {},
    "not-an-arn",
    "arn:aws:cloudwatch:us-east-1",
    "wrong:aws:cloudwatch:us-east-1:123456789012:alarm:A",
    "arn:aws:sns:us-east-1:123456789012:alarm:A",
    "arn:aws:cloudwatch:us-east-1:123456789012:dashboard/A",
    "arn:aws:cloudwatch:us-east-1:123456789012:alarm/A",
    "arn:aws:cloudwatch:us-east-1:123456789012:alarm:",
    "arn:aws:cloudwatch:us-east-1:123456789012:alarm:   ",
    "arn:aws:cloudwatch:us-east-1:123456789012:alarm:A\n",
    "arn:aws:cloudwatch:us-east-1:12345678901:alarm:A",
    "arn:aws:cloudwatch:us-east-1:1234567890123:alarm:A",
    "arn:aws:cloudwatch:us-east-1:12345678901a:alarm:A",
    "arn:aws:cloudwatch:us-east-1:１２３４５６７８９０１２:alarm:A",
    "arn:aws:cloudwatch::123456789012:alarm:A",
    "arn:aws:cloudwatch:US East (N. Virginia):123456789012:alarm:A",
    "arn:aws:cloudwatch:us-east-1\n:123456789012:alarm:A",
    "arn:aws:cloudwatch:xx-east-1:123456789012:alarm:A",
    "arn:aws:cloudwatch:us-gov-1:123456789012:alarm:A",
    "arn:aws:cloudwatch:us-east-0:123456789012:alarm:A",
    "arn:aws:cloudwatch:us-east-01:123456789012:alarm:A",
    "arn:aws:cloudwatch:us-gov-west-1:123456789012:alarm:A",
    "arn:aws:cloudwatch:cn-north-1:123456789012:alarm:A",
    "arn:aws-us-gov:cloudwatch:us-east-1:123456789012:alarm:A",
    "arn:aws-us-gov:cloudwatch:cn-north-1:123456789012:alarm:A",
    "arn:aws-cn:cloudwatch:us-east-1:123456789012:alarm:A",
    "arn:aws-cn:cloudwatch:us-gov-west-1:123456789012:alarm:A",
    "arn:aws-iso:cloudwatch:us-iso-east-1:123456789012:alarm:A",
    "arn:aws-eusc:cloudwatch:eusc-de-east-1:123456789012:alarm:A",
    "arn:unknown:cloudwatch:us-east-1:123456789012:alarm:A",
]


def test_actual_sns_label_normalizes_scope_without_mutating_source(monkeypatch):
    """The observed SNS label must not override its valid ARN or survive in prompt scope."""
    monkeypatch.setenv("AWS_REGION", "eu-west-3")
    original = deepcopy(SNS_ALARM)
    envelope = json.dumps({"Type": "Notification", "Message": json.dumps(original)})
    decoded = parse_sns_envelope(envelope)

    alarm = parse_alarm(decoded)

    assert alarm.region == "us-east-1"
    assert decoded == original == SNS_ALARM
    assert decoded["Region"] == "US East (N. Virginia)"
    assert alarm.alarm_description == original["AlarmDescription"]
    assert alarm.dimensions == {"ServiceName": "healthcare-sensor-app"}
    assert alarm.datapoints_to_alarm == 2
    assert alarm.eval_source_metadata is None
    for role in ("rca", "report"):
        assert "- **리전**: us-east-1" in build_prompt(alarm, role=role).splitlines()


@pytest.mark.parametrize("partition,region", SUPPORTED_REGIONS)
@pytest.mark.parametrize("with_arn", [False, True])
def test_canonical_region_is_preserved(partition, region, with_arn, monkeypatch):
    """Explicit canonical scope wins over the runtime and agrees with a matching ARN."""
    monkeypatch.setenv("AWS_REGION", "eu-west-3")
    data = {"Region": region}
    if with_arn:
        data["AlarmArn"] = f"arn:{partition}:cloudwatch:{region}:123456789012:alarm:A"
    assert parse_alarm(data).region == region


@pytest.mark.parametrize("partition,region", SUPPORTED_REGIONS)
@pytest.mark.parametrize(
    "supplied", [{}, {"Region": None}, {"Region": ""}, {"Region": " \t\n"}, {"Region": "SNS display label"}]
)
def test_supported_arn_resolves_missing_or_noncanonical_region(partition, region, supplied, monkeypatch):
    """Each supported partition can supply scope, including alarm names containing colons."""
    monkeypatch.setenv("AWS_REGION", "eu-west-3")
    data = {**supplied, "AlarmArn": f"arn:{partition}:cloudwatch:{region}:123456789012:alarm:A:with:colons"}
    assert parse_alarm(data).region == region


@pytest.mark.parametrize("arn", INVALID_ARNS)
@pytest.mark.parametrize("canonical", [False, True])
def test_invalid_arn_cannot_supply_scope_or_create_a_conflict(arn, canonical, monkeypatch):
    """Invalid ARNs preserve canonical regions but cannot resolve explicit display labels."""
    monkeypatch.setenv("AWS_REGION", "eu-west-3")
    data = {"Region": "ap-northeast-2" if canonical else "US East (N. Virginia)", "AlarmArn": arn}
    original = deepcopy(data)
    if canonical:
        assert parse_alarm(data).region == "ap-northeast-2"
    else:
        with pytest.raises(ValueError, match="Region .* cannot be resolved"):
            parse_alarm(data)
    assert data == original


@pytest.mark.parametrize(
    "supplied",
    [{}, *({"Region": value} for value in (None, "", "  ", "\t\n"))],
)
@pytest.mark.parametrize("configured", [None, "eu-west-3", "us-gov-west-1", "cn-north-1", "", "US East (N. Virginia)"])
@pytest.mark.parametrize("arn_fields", [{}, {"AlarmArn": "not-an-arn"}])
def test_absent_or_blank_region_uses_only_canonical_environment_or_default(
    supplied, configured, arn_fields, monkeypatch
):
    """Absent/null/blank regions retain historical fallback when no valid ARN supplies scope."""
    if configured is None:
        monkeypatch.delenv("AWS_REGION", raising=False)
    else:
        monkeypatch.setenv("AWS_REGION", configured)
    expected = configured if configured in ("eu-west-3", "us-gov-west-1", "cn-north-1") else "us-east-1"
    data = {**supplied, **arn_fields}
    original = deepcopy(data)
    assert parse_alarm(data).region == expected
    assert data == original


@pytest.mark.parametrize(
    "region", ["US East (N. Virginia)", "xx-east-1", "eusc-de-east-1", "us-iso-east-1", "us-east-1\n"]
)
@pytest.mark.parametrize("arn_fields", [{}, {"AlarmArn": "not-an-arn"}])
@pytest.mark.parametrize("configured", [None, "eu-west-3"])
def test_explicit_unresolved_region_rejects_without_runtime_redirection(region, arn_fields, configured, monkeypatch):
    """Unsupported/future syntax and unresolved labels cannot silently route to runtime scope."""
    if configured is None:
        monkeypatch.delenv("AWS_REGION", raising=False)
    else:
        monkeypatch.setenv("AWS_REGION", configured)
    data = {"Region": region, **arn_fields}
    original = deepcopy(data)
    with pytest.raises(ValueError, match="Region .* cannot be resolved"):
        parse_alarm(data)
    assert data == original


@pytest.mark.parametrize("region", [0, 1, False, True, {}, [], ["us-east-1"]])
@pytest.mark.parametrize("arn_fields", [{}, {"AlarmArn": "not-an-arn"}, {"AlarmArn": SNS_ALARM["AlarmArn"]}])
@pytest.mark.parametrize("configured", [None, "eu-west-3"])
def test_malformed_region_types_reject_even_with_valid_arn(region, arn_fields, configured, monkeypatch):
    """Malformed explicit types are not missing metadata and cannot be rescued by ARN or runtime."""
    if configured is None:
        monkeypatch.delenv("AWS_REGION", raising=False)
    else:
        monkeypatch.setenv("AWS_REGION", configured)
    data = {"Region": region, **arn_fields}
    original = deepcopy(data)
    with pytest.raises(ValueError, match="Region must be a string or null"):
        parse_alarm(data)
    assert data == original


@pytest.mark.parametrize(
    "region,partition,arn_region",
    [
        ("ap-northeast-2", "aws", "us-east-1"),
        ("us-gov-east-1", "aws-us-gov", "us-gov-west-1"),
        ("cn-north-1", "aws-cn", "cn-northwest-1"),
        ("us-east-1", "aws-cn", "cn-north-1"),
        ("cn-north-1", "aws-us-gov", "us-gov-west-1"),
    ],
)
def test_conflicting_canonical_region_and_valid_arn_raise_without_mutation(region, partition, arn_region):
    """Conflicting incident coordinates fail explicitly instead of silently selecting scope."""
    data = {**deepcopy(SNS_ALARM), "Region": region}
    data["AlarmArn"] = f"arn:{partition}:cloudwatch:{arn_region}:123456789012:alarm:A"
    original = deepcopy(data)
    with pytest.raises(ValueError, match="Region .* conflicts with AlarmArn region"):
        parse_alarm(data)
    assert data == original


@pytest.mark.parametrize("supplied", [{}, {"region": None}, {"region": ""}, {"region": "  "}])
def test_eval_missing_region_stays_not_provided_even_with_valid_arn(supplied, monkeypatch):
    """Evaluation source metadata must not gain scope from an ARN or the runtime."""
    monkeypatch.setenv("AWS_REGION", "eu-west-3")
    scenario = {
        "executionModes": ["model-eval"],
        "alarm": {"name": "A", "metric": "M", "arn": SNS_ALARM["AlarmArn"], **supplied},
    }
    original = deepcopy(scenario)
    alarm = eval_adapter._alarm_for(scenario)
    assert alarm.region == "not provided"
    assert alarm.eval_source_metadata["arn"] == SNS_ALARM["AlarmArn"]
    for role in ("rca", "report"):
        assert "- **리전**: not provided" in build_prompt(alarm, role=role).splitlines()
    assert scenario == original


def test_eval_supplied_region_and_other_source_values_stay_independent(monkeypatch):
    """Production normalization must not reconcile or fill source-only evaluation fields."""
    monkeypatch.setenv("AWS_REGION", "eu-west-3")
    metadata = {
        "region": "ap-northeast-2",
        "arn": SNS_ALARM["AlarmArn"],
        "dimensions": {},
        "period": 0,
        "evaluationPeriods": 2,
    }
    scenario = {"executionModes": ["model-eval"], "alarm": {"name": "A", "metric": "M", **metadata}}
    original = deepcopy(scenario)
    alarm = eval_adapter._alarm_for(scenario)
    assert alarm.region == "ap-northeast-2"
    assert alarm.eval_source_metadata == {**metadata, "metric": "M"}
    assert alarm.period == 0
    assert alarm.datapoints_to_alarm is None
    for role in ("rca", "report"):
        lines = build_prompt(alarm, role=role).splitlines()
        assert "- **리전**: ap-northeast-2" in lines
        assert "- **주기**: 0초" in lines
        assert "- **상태 변경 시각**: not provided" in lines
    assert scenario == original
