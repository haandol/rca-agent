from unittest.mock import MagicMock, call, patch

from rca_agent.adapters.secondary.embedding import bedrock_embedding
from rca_agent.config.aws_sdk import (
    AWS_SDK_CALL_WORST_CASE_SECONDS,
    AWS_SDK_CONNECT_TIMEOUT_SECONDS,
    AWS_SDK_READ_TIMEOUT_SECONDS,
    AWS_SDK_TOTAL_MAX_ATTEMPTS,
    EVIDENCE_SAVE_BACKOFF_WORST_CASE_SECONDS,
    EVIDENCE_SAVE_BASE_DELAY_SECONDS,
    EVIDENCE_SAVE_WORST_CASE_SECONDS,
    NOTIFICATION_BACKOFF_WORST_CASE_SECONDS,
    NOTIFICATION_BASE_DELAY_SECONDS,
    NOTIFICATION_MAX_RETRIES,
    NOTIFICATION_PUBLISH_WORST_CASE_SECONDS,
    PLAYBOOK_SAVE_WORST_CASE_SECONDS,
    PUBLICATION_LEASE_SAFETY_MARGIN_SECONDS,
    PUBLICATION_LEASE_SECONDS,
    PUBLICATION_MARK_ATTEMPTS,
    PUBLICATION_MARK_RETRY_DELAY_SECONDS,
    PUBLICATION_WORST_CASE_SECONDS,
    REMEDIATION_CLAIM_SAFETY_MARGIN_SECONDS,
    REMEDIATION_CLAIM_SECONDS,
    REMEDIATION_EXECUTION_WORST_CASE_SECONDS,
    SIDE_EFFECT_AWS_CLIENT_CONFIG,
    SIDE_EFFECT_LEASE_SAFETY_MARGIN_SECONDS,
    SIDE_EFFECT_LEASE_SECONDS,
    SIDE_EFFECT_SAVE_WORST_CASE_SECONDS,
)
from rca_agent.config.settings import (
    LLM_DEFAULT_TIMEOUT_SECONDS,
    REMEDIATION_RESET_TIMEOUT_SECONDS,
    REMEDIATION_VERIFICATION_MAX_WAIT_SECONDS,
    S3_EVIDENCE_MAX_RETRIES,
)
from rca_agent.di import app_container
from rca_agent.di.app_container import AppContainer
from rca_agent.services.pipeline import PipelineOrchestrator


def test_app_container_bounds_s3_and_s3_vectors_sdk_calls(monkeypatch):
    monkeypatch.setattr(app_container, "S3_EVIDENCE_BUCKET", "evidence-bucket")
    monkeypatch.setattr(app_container, "S3_VECTOR_BUCKET_NAME", "vector-bucket")
    s3 = MagicMock(name="s3")
    s3_vectors = MagicMock(name="s3-vectors")

    with patch.object(
        app_container.boto3,
        "client",
        side_effect=[s3, s3_vectors],
    ) as client:
        container = AppContainer("https://sqs.example.test/rca")

        assert container.s3_client is s3
        assert container.s3_vectors_client is s3_vectors

    assert client.call_args_list == [
        call("s3", config=SIDE_EFFECT_AWS_CLIENT_CONFIG),
        call(
            "s3vectors",
            region_name=app_container.S3_VECTOR_REGION,
            config=SIDE_EFFECT_AWS_CLIENT_CONFIG,
        ),
    ]


def test_app_container_bounds_cloudwatch_sdk_calls():
    default_cloudwatch = MagicMock(name="default-cloudwatch")
    regional_cloudwatch = MagicMock(name="regional-cloudwatch")

    with patch.object(
        app_container.boto3,
        "client",
        side_effect=[default_cloudwatch, regional_cloudwatch],
    ) as client:
        container = AppContainer("https://sqs.example.test/rca")

        assert container.cloudwatch_client is default_cloudwatch
        assert container.cloudwatch_client is default_cloudwatch
        assert container.cloudwatch_client_for_region("ap-northeast-2") is regional_cloudwatch
        assert container.cloudwatch_client_for_region("ap-northeast-2") is regional_cloudwatch

    assert client.call_args_list == [
        call(
            "cloudwatch",
            config=SIDE_EFFECT_AWS_CLIENT_CONFIG,
        ),
        call(
            "cloudwatch",
            config=SIDE_EFFECT_AWS_CLIENT_CONFIG,
            region_name="ap-northeast-2",
        ),
    ]


def test_app_container_bounds_dynamodb_and_sns_sdk_calls(monkeypatch):
    monkeypatch.setattr(app_container, "DYNAMODB_TABLE_NAME", "rca-sessions")
    monkeypatch.setattr(
        app_container,
        "SNS_NOTIFICATION_TOPIC_ARN",
        "arn:aws:sns:us-east-1:123456789012:rca",
    )
    dynamodb = MagicMock(name="dynamodb")
    sns = MagicMock(name="sns")

    with patch.object(
        app_container.boto3,
        "client",
        side_effect=[dynamodb, sns],
    ) as client:
        container = AppContainer("https://sqs.example.test/rca")

        assert container.dynamodb_client is dynamodb
        assert container.sns_client is sns

    assert client.call_args_list == [
        call("dynamodb", config=SIDE_EFFECT_AWS_CLIENT_CONFIG),
        call("sns", config=SIDE_EFFECT_AWS_CLIENT_CONFIG),
    ]


def test_bedrock_embedding_client_has_bounded_sdk_calls():
    client = MagicMock()

    with patch.object(
        bedrock_embedding.boto3,
        "client",
        return_value=client,
    ) as create_client:
        adapter = bedrock_embedding.BedrockEmbeddingAdapter()

        assert adapter.client is client

    create_client.assert_called_once_with(
        "bedrock-runtime",
        region_name=bedrock_embedding.BEDROCK_REGION,
        config=SIDE_EFFECT_AWS_CLIENT_CONFIG,
    )


def test_side_effect_sdk_config_disables_implicit_retries():
    assert SIDE_EFFECT_AWS_CLIENT_CONFIG.connect_timeout == AWS_SDK_CONNECT_TIMEOUT_SECONDS
    assert SIDE_EFFECT_AWS_CLIENT_CONFIG.read_timeout == AWS_SDK_READ_TIMEOUT_SECONDS
    assert SIDE_EFFECT_AWS_CLIENT_CONFIG.retries == {
        "mode": "standard",
        "total_max_attempts": AWS_SDK_TOTAL_MAX_ATTEMPTS,
    }
    assert AWS_SDK_TOTAL_MAX_ATTEMPTS == 1


def test_pipeline_uses_the_bounded_side_effect_lease():
    store = MagicMock()
    store.acquire_side_effect_lease.return_value = "lease-1"
    store.release_side_effect_lease.return_value = True
    orchestrator = PipelineOrchestrator(MagicMock(session_store=store))

    with orchestrator._side_effect_lease("rca-1", "claim-1", "playbook"):
        pass

    store.acquire_side_effect_lease.assert_called_once_with(
        "rca-1",
        "claim-1",
        "playbook",
        lease_seconds=SIDE_EFFECT_LEASE_SECONDS,
    )


def test_side_effect_lease_exceeds_all_save_time_bounds():
    expected_call_bound = AWS_SDK_CONNECT_TIMEOUT_SECONDS + AWS_SDK_READ_TIMEOUT_SECONDS
    expected_backoff_bound = EVIDENCE_SAVE_BASE_DELAY_SECONDS * (2 ** (S3_EVIDENCE_MAX_RETRIES - 1) - 1)
    expected_evidence_bound = S3_EVIDENCE_MAX_RETRIES * expected_call_bound + expected_backoff_bound

    assert expected_call_bound == AWS_SDK_CALL_WORST_CASE_SECONDS
    assert expected_backoff_bound == EVIDENCE_SAVE_BACKOFF_WORST_CASE_SECONDS
    assert expected_evidence_bound == EVIDENCE_SAVE_WORST_CASE_SECONDS
    assert 2 * expected_call_bound == PLAYBOOK_SAVE_WORST_CASE_SECONDS
    assert (
        max(
            expected_evidence_bound,
            2 * expected_call_bound,
        )
        == SIDE_EFFECT_SAVE_WORST_CASE_SECONDS
    )
    assert SIDE_EFFECT_LEASE_SECONDS >= SIDE_EFFECT_SAVE_WORST_CASE_SECONDS + SIDE_EFFECT_LEASE_SAFETY_MARGIN_SECONDS


def test_remediation_claim_exceeds_worst_case_execution_bound():
    expected = (
        REMEDIATION_RESET_TIMEOUT_SECONDS
        + REMEDIATION_VERIFICATION_MAX_WAIT_SECONDS
        + 2 * AWS_SDK_CALL_WORST_CASE_SECONDS
        + LLM_DEFAULT_TIMEOUT_SECONDS
        + AWS_SDK_CALL_WORST_CASE_SECONDS
    )

    assert expected == REMEDIATION_EXECUTION_WORST_CASE_SECONDS
    assert expected + REMEDIATION_CLAIM_SAFETY_MARGIN_SECONDS <= REMEDIATION_CLAIM_SECONDS


def test_publication_lease_exceeds_publish_and_sent_mark_bounds():
    expected_backoff = NOTIFICATION_BASE_DELAY_SECONDS * (2 ** (NOTIFICATION_MAX_RETRIES - 1) - 1)
    expected_publish = NOTIFICATION_MAX_RETRIES * AWS_SDK_CALL_WORST_CASE_SECONDS + expected_backoff
    expected_total = (
        expected_publish
        + PUBLICATION_MARK_ATTEMPTS * AWS_SDK_CALL_WORST_CASE_SECONDS
        + (PUBLICATION_MARK_ATTEMPTS - 1) * PUBLICATION_MARK_RETRY_DELAY_SECONDS
    )

    assert expected_backoff == NOTIFICATION_BACKOFF_WORST_CASE_SECONDS
    assert expected_publish == NOTIFICATION_PUBLISH_WORST_CASE_SECONDS
    assert expected_total == PUBLICATION_WORST_CASE_SECONDS
    assert expected_total + PUBLICATION_LEASE_SAFETY_MARGIN_SECONDS <= PUBLICATION_LEASE_SECONDS
