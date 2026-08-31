from __future__ import annotations

import os

CODEX_MODEL = os.environ.get("CODEX_MODEL", "global.openai.gpt-5.6-sol")
CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "high")
CODEX_MODEL_PROVIDER = os.environ.get("CODEX_MODEL_PROVIDER", "amazon-bedrock-runtime")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
CODEX_BEDROCK_BASE_URL = os.environ.get(
    "CODEX_BEDROCK_BASE_URL",
    f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com/openai/v1",
)

SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
SQS_POLL_WAIT_SECONDS = int(os.environ.get("SQS_POLL_WAIT_SECONDS", "20"))

EXECUTION_QUEUE_URL = os.environ.get("EXECUTION_QUEUE_URL", "")
EXECUTION_POLL_WAIT_SECONDS = int(os.environ.get("EXECUTION_POLL_WAIT_SECONDS", "20"))

DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "")
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "90"))

S3_EVIDENCE_BUCKET = os.environ.get("S3_EVIDENCE_BUCKET", "")
S3_REPORT_BUCKET = os.environ.get("S3_REPORT_BUCKET", "")
SNS_NOTIFICATION_TOPIC_ARN = os.environ.get("SNS_NOTIFICATION_TOPIC_ARN", "")
S3_VECTOR_BUCKET_NAME = os.environ.get("S3_VECTOR_BUCKET_NAME", "")
S3_VECTOR_PLAYBOOK_INDEX = os.environ.get("S3_VECTOR_PLAYBOOK_INDEX", "playbook")
S3_VECTOR_REGION = os.environ.get("S3_VECTOR_REGION", "us-east-1")
BEDROCK_EMBEDDING_MODEL_ID = os.environ.get("BEDROCK_EMBEDDING_MODEL_ID", "cohere.embed-v4:0")

# 병합 임계값은 일반 조회보다 엄격하다. 다른 유형의 장애를 같은 플레이북으로 병합하면
# 절차가 뒤섞여 어느 쪽에도 쓸 수 없어지므로, 새로 하나 만드는 것보다 나쁘다.
PLAYBOOK_UPDATE_THRESHOLD = float(os.environ.get("PLAYBOOK_UPDATE_THRESHOLD", "0.80"))
PLAYBOOK_TOP_K = int(os.environ.get("PLAYBOOK_TOP_K", "3"))

PRESIGNED_URL_EXPIRY = 86400

# 이 엔진은 예산이 소진되면 프로세스가 강제 종료되고, 산출물은 실행이 끝나야 저장되므로
# 부분 결과가 남지 않는다 — 예산 초과는 회차 전체의 손실이다. 완주 회차가 24~29분을 쓰는
# 것을 라이브에서 관측했고, 회차 편차만으로 실패하지 않도록 그 두 배를 예산으로 둔다.
CODEX_TIMEOUT_SECONDS = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "3600"))

# Execution includes waiting for the success criteria to become observable, so it
# gets its own budget rather than reusing the analysis timeout.
EXECUTION_TIMEOUT_SECONDS = int(os.environ.get("EXECUTION_TIMEOUT_SECONDS", "3600"))
RETROSPECTIVE_TIMEOUT_SECONDS = int(os.environ.get("RETROSPECTIVE_TIMEOUT_SECONDS", "900"))

# The claim has to outlive the worst-case execution or an in-flight request gets
# redelivered and runs a second time.
EXECUTION_CLAIM_SECONDS = max(
    int(os.environ.get("EXECUTION_CLAIM_SECONDS", "0")) or EXECUTION_TIMEOUT_SECONDS + 900,
    EXECUTION_TIMEOUT_SECONDS + 900,
)

# 이 워커는 한 세션씩 직렬로 처리하므로 한 회차가 예산을 다 쓰면 그만큼의 대기가 다음
# 알람에 그대로 전가된다. 이 기준이 분석 예산보다 짧으면 예산 초과 한 번이 뒤따르는
# 알람을 통째로 폐기하므로, 예산 이상으로 둔다.
ALARM_STALENESS_SECONDS = max(
    int(os.environ.get("ALARM_STALENESS_SECONDS", "0")) or CODEX_TIMEOUT_SECONDS,
    CODEX_TIMEOUT_SECONDS,
)
ACTIVE_INCIDENT_OK_COOLDOWN_SECONDS = int(os.environ.get("ACTIVE_INCIDENT_OK_COOLDOWN_SECONDS", "300"))

# The analysis run holds no side-effect lease of its own beyond the final
# publication, so the lease only has to outlive report/playbook persistence and
# the notification retry.
SIDE_EFFECT_LEASE_SECONDS = min(
    max(int(os.environ.get("SIDE_EFFECT_LEASE_SECONDS", "60")), 60),
    300,
)

ENGINE = "headless-codex"


def validate_codex_model_contract() -> None:
    expected_base_url = f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com/openai/v1"
    expected = {
        "CODEX_MODEL": "global.openai.gpt-5.6-sol",
        "CODEX_REASONING_EFFORT": "high",
        "CODEX_MODEL_PROVIDER": "amazon-bedrock-runtime",
        "CODEX_BEDROCK_BASE_URL": expected_base_url,
    }
    actual = {
        "CODEX_MODEL": CODEX_MODEL,
        "CODEX_REASONING_EFFORT": CODEX_REASONING_EFFORT,
        "CODEX_MODEL_PROVIDER": CODEX_MODEL_PROVIDER,
        "CODEX_BEDROCK_BASE_URL": CODEX_BEDROCK_BASE_URL,
    }
    mismatches = [f"{name}={actual[name]!r}" for name, value in expected.items() if actual[name] != value]
    if mismatches:
        raise RuntimeError(
            "Codex model contract mismatch: "
            + ", ".join(mismatches)
            + ". Expected global.openai.gpt-5.6-sol with reasoning effort high on amazon-bedrock-runtime."
        )
