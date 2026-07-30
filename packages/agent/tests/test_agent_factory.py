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

    def test_returns_empty_when_disabled(self):
        with patch("rca_agent.agent_factory.THINKING_ENABLED", False):
            result = _build_thinking_fields()
        assert result == {}

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

    def test_no_thinking_when_disabled(self):
        with patch("rca_agent.agent_factory.THINKING_ENABLED", False):
            model = create_planning_model()
        assert "additional_request_fields" not in model.config

    def test_defaults_to_the_sonnet_5_generation(self):
        assert create_planning_model().config["model_id"] == "global.anthropic.claude-sonnet-5"

    def test_model_id_is_overridable(self):
        model = create_planning_model(model_id="global.anthropic.claude-sonnet-5")
        assert model.config["model_id"] == "global.anthropic.claude-sonnet-5"


class TestCreateExecutionModel:
    def test_defaults_to_the_sonnet_5_generation(self):
        assert create_execution_model().config["model_id"] == "global.anthropic.claude-sonnet-5"

    def test_no_thinking_fields(self):
        model = create_execution_model()
        assert "additional_request_fields" not in model.config


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
        assert "additional_request_fields" not in agent.model.config

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
        assert "additional_request_fields" not in agent.model.config

    @patch("rca_agent.agent_factory.THINKING_ENABLED", True)
    def test_playbook_uses_planning_tier(self):
        agent = create_playbook_agent()
        assert "sonnet" in agent.model.config["model_id"]
        assert "thinking" in agent.model.config.get("additional_request_fields", {})
