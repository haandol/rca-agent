from datetime import UTC, datetime

import pytest

from rca_agent.ports.dto.models import AlarmPayload, AlarmTrigger
from rca_agent.ports.interfaces.embedding import EmbeddingPort


class FakeEmbedding(EmbeddingPort):
    """Deterministic embedding adapter for tests."""

    def __init__(self, dim: int = 1024):
        self._dim = dim

    def embed_query(self, text: str) -> list[float]:  # noqa: ARG002
        return [0.1] * self._dim

    def embed_document(self, text: str) -> list[float]:  # noqa: ARG002
        return [0.1] * self._dim


@pytest.fixture()
def fake_embedding() -> EmbeddingPort:
    return FakeEmbedding()


@pytest.fixture()
def sample_alarm() -> AlarmPayload:
    return AlarmPayload(
        alarm_name="HighCPU-web-service",
        alarm_arn="arn:aws:cloudwatch:us-east-1:123456789012:alarm:HighCPU-web-service",
        new_state="ALARM",
        new_state_reason="Threshold Crossed: 1 out of 1 datapoints [92.5] was >= 80.0",
        state_change_time=datetime(2026, 4, 22, 10, 30, 0, tzinfo=UTC),
        trigger=AlarmTrigger(
            metric_name="CPUUtilization",
            namespace="AWS/ECS",
            dimensions={"ServiceName": "web-service", "ClusterName": "prod-cluster"},
            statistic="Average",
            period=300,
            threshold=80.0,
            comparison_operator="GreaterThanOrEqualToThreshold",
        ),
        region="us-east-1",
    )
