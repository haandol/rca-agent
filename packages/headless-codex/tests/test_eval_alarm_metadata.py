"""Offline adapter boundary checks; no model, database or AWS calls."""

import json
from dataclasses import asdict

import pytest

from headless_codex import eval_adapter
from headless_codex.ports.dto.models import AlarmContext
from headless_codex.services.prompt_builder import build_prompt

METADATA = {
    "stateChangeTime": "2026-09-10T00:06:00Z",
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
}
FIELDS = {
    "stateChangeTime": "state_change_time",
    "region": "region",
    "namespace": "namespace",
    "dimensions": "dimensions",
    "statistic": "statistic",
    "period": "period",
    "threshold": "threshold",
    "comparisonOperator": "comparison_operator",
    "evaluationPeriods": "evaluation_periods",
    "datapointsToAlarm": "datapoints_to_alarm",
    "treatMissingData": "treat_missing_data",
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


@pytest.mark.parametrize("source,target", FIELDS.items())
def test_each_optional_field_is_preserved_without_filling_others(source, target):
    """Test each optional field is preserved without filling others."""
    alarm = eval_adapter._alarm_for(scenario(**{source: METADATA[source]}))
    expected = asdict(AlarmContext(alarm_name="A", metric_name="M"))
    expected["region"] = "not provided"
    expected["eval_source_metadata"] = {source: METADATA[source], "metric": "M"}
    expected[target] = METADATA[source]
    actual = asdict(alarm)
    actual.pop("state_reason")
    expected.pop("state_reason")
    assert actual == expected
    metadata_text = alarm.state_reason.split("Source alarm metadata (provided values):\n")[1].split("\n\n")[0]
    assert json.loads(metadata_text) == {source: METADATA[source]}


def test_all_metadata_and_source_window_reach_both_role_prompts():
    """Test all metadata and source window reach both role prompts."""
    alarm = eval_adapter._alarm_for(scenario(**METADATA))
    for role in ("rca", "report"):
        prompt = build_prompt(alarm, role=role)
        assert json.dumps(METADATA, ensure_ascii=False) in prompt
        assert scenario()["observations"][0]["summary"] in prompt
        assert "must-not-reach-model" not in prompt
    assert alarm.threshold == 0
    assert alarm.datapoints_to_alarm == 1


def test_absent_and_null_metadata_do_not_create_source_measurements():
    """Test absent and null metadata do not create source measurements."""
    for supplied in ({}, {name: None for name in METADATA}):
        alarm = eval_adapter._alarm_for(scenario(**supplied))
        assert "Source alarm metadata" not in alarm.state_reason
        assert alarm.state_change_time is None
        assert alarm.evaluation_periods is None
        assert alarm.datapoints_to_alarm is None
        assert alarm.threshold is None
        assert alarm.dimensions == {}


def test_empty_dimensions_are_preserved_and_missing_datapoints_are_not_inferred():
    """Test empty dimensions are preserved and missing datapoints are not inferred."""
    alarm = eval_adapter._alarm_for(scenario(dimensions={}, evaluationPeriods=2))
    assert alarm.dimensions == {}
    assert alarm.evaluation_periods == 2
    assert alarm.datapoints_to_alarm is None


def test_provided_arn_is_visible_without_extending_alarm_context():
    """Test provided arn is visible without extending alarm context."""
    arn = "arn:aws:cloudwatch:ap-northeast-2:123456789012:alarm:A"
    alarm = eval_adapter._alarm_for(scenario(arn=arn))
    assert arn in build_prompt(alarm, role="rca")
    assert arn in build_prompt(alarm, role="report")
    assert not hasattr(alarm, "arn")


def test_catalog_projection_reaches_both_roles_without_operator_proof_or_answers():
    """Test catalog projection reaches both roles without operator proof or answers."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    for path in sorted((root / "tests/scenarios").glob("*.json")):
        supplied = json.loads(path.read_text())
        alarm = eval_adapter._alarm_for(supplied)
        for role in ("rca", "report"):
            prompt = build_prompt(alarm, role=role)
            assert supplied["provenance"]["source"] not in prompt
            assert supplied["provenance"]["operatorMapping"] not in prompt
            assert supplied["id"] not in alarm.state_reason
            for marker in ("case=", "phase=", "phases[", "restored_write", "release_events", "rollback_complete"):
                assert marker not in alarm.state_reason
            for observation in supplied["observations"]:
                assert observation["summary"] in prompt


@pytest.mark.parametrize("missing", [None, "", "   "])
@pytest.mark.parametrize("role", ["orchestrator", "rca", "report"])
def test_missing_metadata_is_unknown_in_actual_prompt(missing, role, monkeypatch):
    """All roles distinguish absent incident values from production/runtime defaults."""
    monkeypatch.setenv("AWS_REGION", "eu-west-3")
    supplied = {key: missing for key in METADATA if key != "dimensions"}
    alarm = eval_adapter._alarm_for(scenario(**supplied))
    assert alarm.region == "not provided"
    prompt = build_prompt(alarm, role=role)
    for line in (
        "- **리전**: not provided",
        "- **상태 변경 시각**: not provided",
        "- **메트릭**: not provided/M",
        "- **차원**: not provided",
        "- **통계**: not provided",
        "- **주기**: not provided",
        "- **임계치**: not provided (not provided)",
    ):
        assert line in prompt.splitlines()
    for invented in ("us-east-1", "eu-west-3", "Average", "300초", "arn:aws:"):
        assert invented not in prompt


@pytest.mark.parametrize("role", ["orchestrator", "rca", "report"])
def test_provided_details_and_zero_reach_actual_prompt(role):
    """Provided source values, empty dimensions, and zero never invoke truthy fallbacks."""
    metadata = {**METADATA, "period": 0, "evaluationPeriods": 0, "datapointsToAlarm": 0, "dimensions": {}}
    supplied = scenario(**metadata)
    original = json.dumps(supplied, sort_keys=True)
    alarm = eval_adapter._alarm_for(supplied)
    prompt = build_prompt(alarm, role=role)
    for line in (
        f"- **상태 변경 시각**: {METADATA['stateChangeTime']}",
        "- **리전**: ap-northeast-2",
        "- **메트릭**: Example/Healthcare/M",
        "- **차원**: {}",
        "- **통계**: Sum",
        "- **주기**: 0초",
        "- **임계치**: 0 (GreaterThanThreshold)",
    ):
        assert line in prompt.splitlines()
    assert json.dumps(metadata, ensure_ascii=False) in prompt
    assert "arn:aws:" not in prompt
    assert json.dumps(supplied, sort_keys=True) == original


def test_catalog_missing_region_stays_unknown_in_actual_prompts():
    """All active cases and specialist roles render their original metadata consistently."""
    from pathlib import Path

    paths = sorted((Path(__file__).resolve().parents[3] / "tests/scenarios").glob("*.json"))
    assert len(paths) == 4
    for path in paths:
        supplied = json.loads(path.read_text())
        alarm = eval_adapter._alarm_for(supplied)
        for role in ("rca", "report"):
            prompt = build_prompt(alarm, role=role)
            assert "- **리전**: not provided" in prompt.splitlines()
            assert f"- **통계**: {supplied['alarm']['statistic']}" in prompt.splitlines()
            assert f"- **주기**: {supplied['alarm']['period']}초" in prompt.splitlines()
            assert "us-east-1" not in prompt


def test_production_defaults_remain_compatible(monkeypatch):
    """Production parsing still uses its configured region and existing prompt defaults."""
    from headless_codex.ports.dto.models import parse_alarm

    monkeypatch.setenv("AWS_REGION", "eu-west-3")
    alarm = parse_alarm({"AlarmName": "A", "Trigger": {"MetricName": "M", "EvaluationPeriods": 2}})
    assert alarm.eval_source_metadata is None
    assert alarm.region == "eu-west-3"
    assert alarm.datapoints_to_alarm == 2
    assert AlarmContext().region == "us-east-1"
    for role in ("orchestrator", "rca", "report"):
        prompt = build_prompt(alarm, role=role)
        assert "- **리전**: eu-west-3" in prompt.splitlines()
        assert "- **통계**: Average" in prompt.splitlines()
        assert "- **주기**: 300초" in prompt.splitlines()


def test_non_eval_adapter_does_not_enable_source_overrides():
    """Source-only rendering is explicitly restricted to model-eval scenarios."""
    supplied = scenario()
    supplied["executionModes"] = ["deployed-e2e"]
    alarm = eval_adapter._alarm_for(supplied)
    assert alarm.eval_source_metadata is None
    assert alarm.region == "us-east-1"


@pytest.mark.parametrize(
    "source,line",
    [
        ("stateChangeTime", f"- **상태 변경 시각**: {METADATA['stateChangeTime']}"),
        ("region", "- **리전**: ap-northeast-2"),
        ("namespace", "- **메트릭**: Example/Healthcare/M"),
        ("dimensions", "- **차원**: Service=healthcare, Instance=db-example"),
        ("statistic", "- **통계**: Sum"),
        ("period", "- **주기**: 60초"),
        ("threshold", "- **임계치**: 0 (not provided)"),
        ("comparisonOperator", "- **임계치**: not provided (GreaterThanThreshold)"),
    ],
)
def test_individually_provided_fields_reach_both_actual_prompts(source, line):
    """Each source field is independent of defaults and unrelated missing fields."""
    alarm = eval_adapter._alarm_for(scenario(**{source: METADATA[source]}))
    for role in ("rca", "report"):
        prompt = build_prompt(alarm, role=role)
        assert line in prompt.splitlines()
        if source != "region":
            assert "- **리전**: not provided" in prompt.splitlines()
        assert "us-east-1" not in prompt
        assert "arn:aws:" not in prompt


def test_completely_omitted_metadata_reaches_actual_prompts_as_unknown():
    """Omitted metadata never uses implicit production prompt defaults."""
    alarm = eval_adapter._alarm_for(scenario())
    for role in ("rca", "report"):
        prompt = build_prompt(alarm, role=role)
        assert "- **리전**: not provided" in prompt.splitlines()
        assert "- **주기**: not provided" in prompt.splitlines()
        assert "- **상태 변경 시각**: not provided" in prompt.splitlines()
