"""Reproduce SDK history carry-over and verify candidate isolation without provider calls."""

import json
import logging
from copy import deepcopy
from types import SimpleNamespace

from rca_agent.agent_factory import create_playbook_agent
from rca_agent.prompts.playbook import PLAYBOOK_UPDATE_SYSTEM_PROMPT
from rca_agent.services.playbook_gen import PlaybookOutput, PlaybookUpdateOutput, _invoke_update_agent
from rca_agent.utils.agent_invocation import invoke_agent
from tests.test_agent_invocation import LocalStream


def test_actual_sdk_isolates_candidates_and_preserves_original_history(monkeypatch, caplog):
    """Real SDK request bodies prove old outputs/candidates do not enter a new appraisal."""
    agent = create_playbook_agent()
    agent.callback_handler = lambda **_: None
    packets = []
    config = deepcopy(agent.model.config)
    client = agent.model.client

    def transport(operation, request, context):
        """Emulate only the Bedrock boundary; keep real SDK messages, tools and validation."""
        assert operation.name == "ConverseStream"
        packet = json.loads(request["body"])
        packets.append(packet)
        tool = next(item["toolSpec"]["name"] for item in packet["toolConfig"]["tools"] if "toolSpec" in item)
        result = (
            {"failure_type": "DRAFT_OUTPUT_MARKER", "symptom_pattern": "local fixture", "execution_steps": []}
            if tool == "PlaybookOutput"
            else {"applicable": True, "needs_update": False, "rationale": "local fixture", "evidence": ["observed"]}
        )
        events = [
            {"messageStart": {"role": "assistant"}},
            {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": f"local-{len(packets)}", "name": tool}},
                }
            },
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": json.dumps(result)}}}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "tool_use"}},
            {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}},
        ]
        return SimpleNamespace(status_code=200, headers={}), {"stream": LocalStream(events, delay=0)}

    monkeypatch.setattr(client, "_make_request", transport)
    invoke_agent(agent, "DRAFT_PRIVATE_MARKER", PlaybookOutput, 10)
    # This is the former application pattern, reproduced without any real inference.
    invoke_agent(agent, "PRIOR_CANDIDATE_MARKER", PlaybookUpdateOutput, 10)
    assert "DRAFT_PRIVATE_MARKER" in json.dumps(packets[1]["messages"])
    assert "DRAFT_OUTPUT_MARKER" in json.dumps(packets[1]["messages"])
    original_history = deepcopy(agent.messages)
    with caplog.at_level(logging.INFO):
        _invoke_update_agent(agent, "CURRENT_CANDIDATE_ONE", 10)
        _invoke_update_agent(agent, "CURRENT_CANDIDATE_TWO", 10)
    for number, marker in ((2, "CURRENT_CANDIDATE_ONE"), (3, "CURRENT_CANDIDATE_TWO")):
        wire = json.dumps(packets[number]["messages"])
        assert marker in wire
        assert "DRAFT_PRIVATE_MARKER" not in wire
        assert "DRAFT_OUTPUT_MARKER" not in wire
        assert "PRIOR_CANDIDATE_MARKER" not in wire
        assert len(packets[number]["messages"]) == 1
        assert [block for block in packets[number]["system"] if "text" in block] == [
            {"text": PLAYBOOK_UPDATE_SYSTEM_PROMPT}
        ]
        assert [block for block in packets[number]["system"] if "cachePoint" in block] == [
            {"cachePoint": {"type": "default"}}
        ]
        assert len(packets[number]["toolConfig"]["tools"]) == 1
        assert packets[number]["toolConfig"]["tools"][0]["toolSpec"]["name"] == "PlaybookUpdateOutput"
    assert "CURRENT_CANDIDATE_ONE" not in json.dumps(packets[3]["messages"])
    assert agent.messages == original_history
    assert agent.model.client is client and agent.model.config == config
    events = [
        json.loads(record.getMessage().split("recovery_diagnostic ", 1)[1])
        for record in caplog.records
        if record.getMessage().startswith("recovery_diagnostic ")
    ]
    admitted = [event for event in events if event["event"] == "model_request_admitted"]
    stopped = [event for event in events if event["event"] == "model_message_stopped"]
    assert len(admitted) == len(stopped) == 2
    assert len({event["diagnostic_id"] for event in admitted}) == 2
    assert all(event["request"] == 1 for event in admitted)
    assert all(event["stop_reason"] == "tool_use" for event in stopped)
