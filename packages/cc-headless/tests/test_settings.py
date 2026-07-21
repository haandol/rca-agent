import importlib

from cc_headless.config import settings

_SETTING_ENV_NAMES = (
    "HEALTHCARE_RESET_TIMEOUT_SECONDS",
    "CLOUDWATCH_VERIFY_ATTEMPTS",
    "CLOUDWATCH_VERIFY_INTERVAL_SECONDS",
    "SIDE_EFFECT_LEASE_SECONDS",
)


def _reload_with_defaults(monkeypatch):
    for name in _SETTING_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return importlib.reload(settings)


def test_default_cloudwatch_window_covers_90_second_metric_lag(monkeypatch):
    with monkeypatch.context() as isolated:
        defaults = _reload_with_defaults(isolated)
        assert defaults.CLOUDWATCH_VERIFY_ATTEMPTS == 5
        assert defaults.CLOUDWATCH_VERIFY_INTERVAL_SECONDS == 30
        assert (defaults.CLOUDWATCH_VERIFY_ATTEMPTS - 1) * defaults.CLOUDWATCH_VERIFY_INTERVAL_SECONDS == 120
    importlib.reload(settings)


def test_side_effect_lease_exceeds_reset_and_full_verification_wait(monkeypatch):
    with monkeypatch.context() as isolated:
        defaults = _reload_with_defaults(isolated)
        reset_and_verification_seconds = (
            defaults.HEALTHCARE_RESET_TIMEOUT_SECONDS
            + (defaults.CLOUDWATCH_VERIFY_ATTEMPTS - 1) * defaults.CLOUDWATCH_VERIFY_INTERVAL_SECONDS
        )

        assert reset_and_verification_seconds < defaults.SIDE_EFFECT_LEASE_SECONDS
    importlib.reload(settings)
