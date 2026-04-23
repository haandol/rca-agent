from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config import LLM_DEFAULT_TIMEOUT_SECONDS
from rca_agent.models import (
    AlarmPayload,
    RemediationResult,
    VerificationResult,
)
from rca_agent.prompts import VERIFICATION_USER_PROMPT_TEMPLATE

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)

VERIFICATION_DELAY_SECONDS = 30


class VerificationOutput(BaseModel):
    metrics_normalized: bool = False
    verification_summary: str = ""
    remaining_issues: list[str] = Field(default_factory=list)


def _build_user_prompt(
    alarm: AlarmPayload,
    remediation: RemediationResult,
    seconds_since: int,
) -> str:
    namespace = alarm.trigger.namespace if alarm.trigger else "Unknown"
    metric_name = alarm.trigger.metric_name if alarm.trigger else "Unknown"
    threshold = alarm.trigger.threshold if alarm.trigger else "Unknown"

    action_lines = []
    for a in remediation.actions_taken:
        status = "SUCCESS" if a.success else "FAILED"
        action_lines.append(f"- [{status}] {a.description}")

    return VERIFICATION_USER_PROMPT_TEMPLATE.format(
        alarm_name=alarm.alarm_name,
        namespace=namespace,
        metric_name=metric_name,
        threshold=threshold,
        remediation_summary="\n".join(action_lines) or "No actions taken",
        seconds_since_remediation=seconds_since,
    )


def run_verification(
    *,
    agent: Agent,
    alarm: AlarmPayload,
    remediation: RemediationResult,
    remediation_time: float,
    timeout: int = LLM_DEFAULT_TIMEOUT_SECONDS,
) -> VerificationResult:
    elapsed = int(time.time() - remediation_time)
    if elapsed < VERIFICATION_DELAY_SECONDS:
        wait = VERIFICATION_DELAY_SECONDS - elapsed
        logger.info("Waiting %ds for metrics to stabilize before verification", wait)
        time.sleep(wait)
        elapsed = VERIFICATION_DELAY_SECONDS

    user_prompt = _build_user_prompt(alarm, remediation, elapsed)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                lambda: agent(
                    user_prompt,
                    output_model=VerificationOutput,
                ),
            )
            result = future.result(timeout=timeout)

        output: VerificationOutput = result["output"]
        logger.info(
            "Verification completed",
            extra={"normalized": output.metrics_normalized, "issues": len(output.remaining_issues)},
        )
        return VerificationResult(
            rca_id=remediation.rca_id,
            metrics_normalized=output.metrics_normalized,
            verification_summary=output.verification_summary,
            remaining_issues=output.remaining_issues,
        )
    except (FuturesTimeoutError, Exception) as exc:
        logger.error("Verification failed: %s", exc)
        return VerificationResult(
            rca_id=remediation.rca_id,
            metrics_normalized=False,
            verification_summary=f"Verification failed: {exc}",
        )
