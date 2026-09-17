"""Verify both engine implementations keep the same evidence boundary."""

import ast
from pathlib import Path

from headless_codex.services.recovery_observation import observe_recovery_evidence

pytest_plugins = ["recovery_fixture", "test_recovery_evidence"]


def test_raw_alarm_bridge_collects_the_same_verified_evidence(compatible):
    _, alarm, _, _, s3, logs, ecs = compatible
    result = observe_recovery_evidence(
        {
            "AlarmName": alarm.alarm_name,
            "AlarmArn": alarm.alarm_arn,
            "AlarmDescription": alarm.alarm_description,
            "StateChangeTime": alarm.state_change_time.isoformat(),
            "Region": alarm.region,
            "Trigger": {
                "MetricName": alarm.trigger.metric_name,
                "Namespace": alarm.trigger.namespace,
                "Dimensions": [{"name": k, "value": v} for k, v in alarm.trigger.dimensions.items()],
            },
        },
        s3_client=s3,
        logs_client_for_region=lambda _: logs,
        ecs_client_for_region=lambda _: ecs,
        evidence_bucket="evidence",
        timeout_seconds=90,
    )
    assert result["verification"]["status"] == "VERIFIED"
    assert result["context"]["normal"]["task_definition_arn"].endswith(":1")


def test_validation_and_reader_bodies_are_portable_mirrors():
    root = Path(__file__).resolve().parents[3]
    agent = root / "packages/agent/src/rca_agent"
    headless = root / "packages/headless-codex/src/headless_codex/services"

    class Portable(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return None

        def visit_Import(self, node):
            return None

        def visit_arg(self, node):
            node.annotation = None
            return node

    def bodies(path):
        tree = Portable().visit(ast.parse(path.read_text()))
        return {
            node.name: ast.dump(node, include_attributes=False)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        }

    for path in (
        agent / "services/recovery_evidence.py",
        agent / "adapters/secondary/evidence/incident_observation.py",
        agent / "utils/observation_facts.py",
        agent / "ports/dto/observations.py",
    ):
        target = headless / (
            "recovery_evidence.py" if path.name == "recovery_evidence.py" else "recovery_observation.py"
        )
        expected, actual = bodies(path), bodies(target)
        for name, body in expected.items():
            assert actual[name] == body, f"cross-engine recovery drift: {name}"
    expected = bodies(agent / "services/deployment_baseline.py")
    actual = bodies(headless / "recovery_observation.py")
    for name in ("build_rollback_context", "normal_write_accounting"):
        assert actual[name] == expected[name], f"cross-engine baseline drift: {name}"
