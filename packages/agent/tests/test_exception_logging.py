"""Transport/admission failures must be diagnosable without echoing sensitive exception input."""

import logging
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ConnectionClosedError
from urllib3.exceptions import ProtocolError

from rca_agent.utils.exception_logging import safe_exception_info


def test_type_errno_and_frames_survive_without_sensitive_exception_values(caplog):
    logger = logging.getLogger("safe-diagnostic-test")
    secret = "PRIVATE_MODEL_COMMAND_AND_TOKEN"
    try:
        try:
            raise ConnectionResetError(104, secret)
        except ConnectionResetError as reset:
            raise ProtocolError(secret) from reset
    except ProtocolError as error:
        with caplog.at_level(logging.WARNING):
            logger.warning("failed; exception_type=%s", type(error).__name__, exc_info=safe_exception_info(error))
    assert "ProtocolError" in caplog.text and "ConnectionResetError" in caplog.text
    assert "104" in caplog.text and "Traceback" in caplog.text
    assert secret not in caplog.text


@pytest.mark.parametrize("stage", ["scoping", "report", "playbook"])
@pytest.mark.parametrize("failure", ["transport", "admission"])
def test_real_stage_catch_logs_exception_type_and_stack(stage, failure, caplog):
    import time

    from rca_agent.ports.dto.models import AlarmPayload, RcaReport, ScopingResult
    from rca_agent.services.playbook_gen import _generate_draft
    from rca_agent.services.report import run_report_generation
    from rca_agent.services.scoping import run_scoping
    from rca_agent.utils.agent_invocation import InvocationNotStartedError

    error = (
        ConnectionClosedError(endpoint_url="https://example.invalid/PRIVATE_ENDPOINT_TOKEN")
        if failure == "transport"
        else InvocationNotStartedError("PRIVATE_ENDPOINT_TOKEN")
    )
    scope = ScopingResult(alarm_summary="local")
    with caplog.at_level(logging.WARNING):
        if stage == "scoping":
            store = MagicMock()
            store.search_similar.return_value = []
            with patch("rca_agent.services.scoping.invoke_agent", side_effect=error):
                run_scoping(AlarmPayload(alarm_name="local"), MagicMock(), report_store=store)
        elif stage == "report":
            with patch("rca_agent.services.report.invoke_agent", side_effect=error):
                run_report_generation(
                    scope,
                    None,
                    False,
                    hypothesis_path=[],
                    evidence_texts=[],
                    rejected_descriptions=[],
                    timeline=[],
                    agent=MagicMock(),
                )
        else:
            report = RcaReport(rca_id="local", incident_summary="local", root_cause="unknown", confidence_score=0)
            with patch("rca_agent.services.playbook_gen.invoke_agent", side_effect=error):
                _generate_draft(report, MagicMock(), time.monotonic() + 10)
    assert f"exception_type={type(error).__name__}" in caplog.text
    assert "Traceback" in caplog.text
    assert "PRIVATE_ENDPOINT_TOKEN" not in caplog.text
