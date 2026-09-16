from unittest.mock import patch

from rca_agent.agent_factory import (
    _build_thinking_fields,
    create_execution_model,
    create_hypothesis_generation_agent,
    create_planning_model,
    create_playbook_agent,
    create_prioritization_agent,
    create_scoping_agent,
    create_validation_agent,
)


class TestBuildThinkingFields:
    def test_returns_adaptive_when_enabled(self):
        with patch("rca_agent.agent_factory.THINKING_ENABLED", True):
            result = _build_thinking_fields()
        assert result == {"thinking": {"type": "adaptive"}}

    def test_returns_explicit_disabled_when_disabled(self):
        with patch("rca_agent.agent_factory.THINKING_ENABLED", False):
            result = _build_thinking_fields()
        assert result == {"thinking": {"type": "disabled"}}

    def test_never_declares_an_effort_level(self):
        # The target Bedrock deployment rejects `effort`, so the tier split is
        # expressed by adaptive on/off alone.
        with patch("rca_agent.agent_factory.THINKING_ENABLED", True):
            result = _build_thinking_fields()
        assert "effort" not in result
        assert "effort" not in result["thinking"]


class TestCreatePlanningModel:
    def test_includes_thinking_when_enabled(self):
        with patch("rca_agent.agent_factory.THINKING_ENABLED", True):
            model = create_planning_model()
        config = model.config
        assert config["additional_request_fields"]["thinking"]["type"] == "adaptive"
        assert "budget_tokens" not in config["additional_request_fields"]["thinking"]

    def test_explicitly_disables_thinking_when_disabled(self):
        with patch("rca_agent.agent_factory.THINKING_ENABLED", False):
            model = create_planning_model()
        assert model.config["additional_request_fields"] == {"thinking": {"type": "disabled"}}

    def test_defaults_to_the_sonnet_5_generation(self):
        assert create_planning_model().config["model_id"] == "global.anthropic.claude-sonnet-5"

    def test_model_id_is_overridable(self):
        model = create_planning_model(model_id="global.anthropic.claude-sonnet-5")
        assert model.config["model_id"] == "global.anthropic.claude-sonnet-5"


class TestCreateExecutionModel:
    def test_defaults_to_the_sonnet_5_generation(self):
        assert create_execution_model().config["model_id"] == "global.anthropic.claude-sonnet-5"

    def test_explicit_disabled_thinking_fields(self):
        model = create_execution_model()
        assert model.config["additional_request_fields"] == {"thinking": {"type": "disabled"}}


class TestSamplingParameters:
    """This model generation rejects `temperature`, so no tier may send one."""

    def test_planning_model_sends_no_sampling_parameters(self):
        with patch("rca_agent.agent_factory.THINKING_ENABLED", True):
            config = create_planning_model().config
        assert "temperature" not in config
        assert "top_p" not in config
        assert "top_k" not in config

    def test_execution_model_sends_no_sampling_parameters(self):
        config = create_execution_model().config
        assert "temperature" not in config
        assert "top_p" not in config
        assert "top_k" not in config


class TestAgentTierMapping:
    @patch("rca_agent.agent_factory.THINKING_ENABLED", True)
    def test_scoping_uses_execution_tier(self):
        agent = create_scoping_agent()
        # Execution 티어는 thinking 없이 같은 모델을 호출한다.
        assert "sonnet" in agent.model.config["model_id"]
        assert agent.model.config["additional_request_fields"] == {"thinking": {"type": "disabled"}}

    @patch("rca_agent.agent_factory.THINKING_ENABLED", True)
    def test_hypothesis_uses_planning_tier(self):
        agent = create_hypothesis_generation_agent()
        assert "sonnet" in agent.model.config["model_id"]
        assert agent.model.config["additional_request_fields"]["thinking"]["type"] == "adaptive"

    @patch("rca_agent.agent_factory.THINKING_ENABLED", True)
    def test_prioritization_uses_planning_tier(self):
        agent = create_prioritization_agent()
        assert "sonnet" in agent.model.config["model_id"]
        assert "thinking" in agent.model.config.get("additional_request_fields", {})

    @patch("rca_agent.agent_factory.THINKING_ENABLED", True)
    def test_validation_uses_execution_tier(self):
        agent = create_validation_agent()
        assert "sonnet" in agent.model.config["model_id"]
        assert agent.model.config["additional_request_fields"] == {"thinking": {"type": "disabled"}}

    @patch("rca_agent.agent_factory.THINKING_ENABLED", True)
    def test_playbook_uses_planning_tier(self):
        agent = create_playbook_agent()
        assert "sonnet" in agent.model.config["model_id"]
        assert "thinking" in agent.model.config.get("additional_request_fields", {})


class TestActualConverseThinkingParameters:
    """Check the real Strands request builder, not just constructor configuration."""

    def _assert_wire(self, factory, thinking_enabled, expected):
        """Replace only the remote Converse call while retaining the actual model/invocation path."""
        from strands import Agent

        from rca_agent.utils.agent_invocation import invoke_agent

        with patch("rca_agent.agent_factory.THINKING_ENABLED", thinking_enabled):
            model = factory()
        response = {
            "stream": iter(
                [
                    {"messageStart": {"role": "assistant"}},
                    {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "ok"}}},
                    {"contentBlockStop": {"contentBlockIndex": 0}},
                    {"messageStop": {"stopReason": "end_turn"}},
                    {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}},
                ]
            )
        }
        with patch.object(model.client, "converse_stream", return_value=response) as converse:
            invoke_agent(Agent(model=model, callback_handler=None), "parameter test", None, 120)
        converse.assert_called_once()
        params = converse.call_args.kwargs
        assert params["modelId"] == "global.anthropic.claude-sonnet-5"
        assert params["additionalModelRequestFields"] == {"thinking": {"type": expected}}
        assert params["inferenceConfig"]["maxTokens"] == model.config["max_tokens"]
        assert not {"temperature", "topP", "topK"} & params["inferenceConfig"].keys()
        assert "effort" not in str(params["additionalModelRequestFields"])

    def test_execution_explicitly_disables_thinking_on_wire(self):
        self._assert_wire(create_execution_model, True, "disabled")

    def test_planning_disabled_flag_is_explicit_on_wire(self):
        self._assert_wire(create_planning_model, False, "disabled")

    def test_planning_adaptive_is_preserved_on_wire(self):
        self._assert_wire(create_planning_model, True, "adaptive")


def test_approved_socket_and_output_configuration_preserves_model_and_thinking_contract():
    """The larger output and idle limits never become an active-response wallclock deadline."""
    from rca_agent.config import settings
    from rca_agent.config.aws_sdk import SIDE_EFFECT_AWS_CLIENT_CONFIG

    assert settings.SCOPING_TIMEOUT_SECONDS == settings.HYPOTHESIS_GENERATION_TIMEOUT_SECONDS == 900
    assert settings.LLM_DEFAULT_TIMEOUT_SECONDS == 900
    assert settings.EVIDENCE_COLLECTION_TIMEOUT_SECONDS == 1800
    assert settings.RCA_TIME_BUDGET_SECONDS == 3600
    assert SIDE_EFFECT_AWS_CLIENT_CONFIG.read_timeout == 60
    for factory in (create_planning_model, create_execution_model):
        model = factory()
        assert model.config["streaming"] is True
        assert model.config["max_tokens"] == 65536
        assert model.client.meta.config.read_timeout == 300
        assert model.client.meta.config.tcp_keepalive is True
        assert model.client.meta.config.retries["total_max_attempts"] == 1


def test_alarm_staleness_default_is_three_hours_without_environment_override(monkeypatch):
    """Check the actual fallback so a deployed override cannot hide default configuration drift."""
    from runpy import run_path

    from rca_agent.config import settings

    monkeypatch.delenv("ALARM_STALENESS_SECONDS", raising=False)
    assert run_path(settings.__file__)["ALARM_STALENESS_SECONDS"] == 10800


def test_forced_tool_packet_preserves_disabled_without_changing_other_fields():
    """The narrow adapter repairs the installed SDK's removal of explicit disabled thinking."""
    model = create_execution_model()
    model.config["additional_request_fields"]["existing_field"] = "preserved"
    tools = [{"name": "emit", "description": "test", "inputSchema": {"json": {"type": "object"}}}]
    for choice in (None, {"auto": {}}, {"any": {}}, {"tool": {"name": "emit"}}):
        packet = model.format_request(
            [{"role": "user", "content": [{"text": "test"}]}], tools, [{"text": "system"}], choice
        )
        assert packet["additionalModelRequestFields"] == {
            "thinking": {"type": "disabled"},
            "existing_field": "preserved",
        }
        assert not {"temperature", "topP", "topK"} & packet["inferenceConfig"].keys()
        assert "effort" not in str(packet["additionalModelRequestFields"])
