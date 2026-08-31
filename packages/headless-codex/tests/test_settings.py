import importlib

from headless_codex.config import settings

_SETTING_ENV_NAMES = (
    "ACTIVE_INCIDENT_OK_COOLDOWN_SECONDS",
    "SIDE_EFFECT_LEASE_SECONDS",
    "AWS_REGION",
    "CODEX_MODEL",
    "CODEX_REASONING_EFFORT",
    "CODEX_MODEL_PROVIDER",
    "CODEX_BEDROCK_BASE_URL",
)


def _reload_with_defaults(monkeypatch):
    for name in _SETTING_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return importlib.reload(settings)


def test_analysis_settings_carry_no_recovery_configuration(monkeypatch):
    # Recovery moved to its own stack. A reset host or verification window left
    # here would imply this run can act on a service, which it cannot.
    with monkeypatch.context() as isolated:
        defaults = _reload_with_defaults(isolated)

        leftovers = [name for name in dir(defaults) if "HEALTHCARE" in name or "CLOUDWATCH" in name]

        assert leftovers == []
    importlib.reload(settings)


def test_side_effect_lease_outlives_report_persistence_and_notification(monkeypatch):
    # The only side effect this run holds a lease for is the final publication,
    # so the lease has to outlive artifact persistence plus notification retries.
    with monkeypatch.context() as isolated:
        defaults = _reload_with_defaults(isolated)

        assert defaults.SIDE_EFFECT_LEASE_SECONDS >= 60
        assert defaults.SIDE_EFFECT_LEASE_SECONDS <= 300
    importlib.reload(settings)


def test_side_effect_lease_cannot_be_configured_below_its_floor(monkeypatch):
    with monkeypatch.context() as isolated:
        isolated.setenv("SIDE_EFFECT_LEASE_SECONDS", "1")
        defaults = importlib.reload(settings)

        assert defaults.SIDE_EFFECT_LEASE_SECONDS == 60
    importlib.reload(settings)


def test_active_incident_cooldown_defaults_to_five_minutes(monkeypatch):
    with monkeypatch.context() as isolated:
        defaults = _reload_with_defaults(isolated)

        assert defaults.ACTIVE_INCIDENT_OK_COOLDOWN_SECONDS == 300
    importlib.reload(settings)


def test_codex_model_contract_accepts_only_the_bedrock_runtime_endpoint(monkeypatch):
    with monkeypatch.context() as isolated:
        defaults = _reload_with_defaults(isolated)
        defaults.validate_codex_model_contract()

        isolated.setenv("CODEX_BEDROCK_BASE_URL", "https://example.com/openai/v1")
        overridden = importlib.reload(settings)

        try:
            overridden.validate_codex_model_contract()
        except RuntimeError as error:
            assert "CODEX_BEDROCK_BASE_URL" in str(error)
        else:
            raise AssertionError("a non-Bedrock endpoint must be rejected")
    importlib.reload(settings)
