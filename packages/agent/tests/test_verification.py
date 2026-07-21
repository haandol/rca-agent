from rca_agent.ports.dto.models import AlarmPayload, AlarmTrigger, RemediationResult
from rca_agent.services.verification import _build_user_prompt


def test_verification_prompt_preserves_full_alarm_metric_context():
    alarm = AlarmPayload(
        alarm_name="RdsHighConnections",
        region="ap-northeast-2",
        trigger=AlarmTrigger(
            metric_name="DatabaseConnections",
            namespace="AWS/RDS",
            dimensions={"DBInstanceIdentifier": "healthcare-db"},
            statistic="Maximum",
            period=60,
            threshold=30,
            comparison_operator="GreaterThanThreshold",
        ),
    )

    prompt = _build_user_prompt(
        alarm,
        RemediationResult(rca_id="rca-1"),
        30,
    )

    assert "ap-northeast-2" in prompt
    assert '"DBInstanceIdentifier": "healthcare-db"' in prompt
    assert "Maximum" in prompt
    assert "60s" in prompt
    assert "GreaterThanThreshold" in prompt
