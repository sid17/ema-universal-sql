"""Settings read the environment once, with documented defaults."""

import pytest

from src.config import Settings, get_settings

ENV_VARS = (
    "JWT_SECRET",
    "JWT_AUDIENCE",
    "DATABASE_URL",
    "REDIS_URL",
    "REQUEST_TIMEOUT_MS",
    "CONTROL_PLANE_TTL_MS",
    "CACHE_TTL_MS",
    "TEST_MODE",
)


@pytest.fixture
def clean_env(monkeypatch):
    """An environment with every settings variable removed."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def _settings() -> Settings:
    """Build settings ignoring any developer ``.env`` so the test is hermetic."""
    return Settings(_env_file=None)


def test_defaults_apply_when_env_is_empty(clean_env):
    settings = _settings()
    assert settings.JWT_SECRET == "dev-only-signing-key-not-for-production"
    # The property that matters, not the literal: HS256 needs >= 32 bytes
    # or PyJWT warns (RFC 7518 §3.2).
    assert len(settings.JWT_SECRET.encode()) >= 32
    assert settings.JWT_AUDIENCE == "ema-universal-sql"
    assert settings.REQUEST_TIMEOUT_MS == 5000
    assert settings.CONTROL_PLANE_TTL_MS == 30000
    assert settings.CACHE_TTL_MS == 300000
    assert settings.TEST_MODE is False
    assert settings.DATABASE_URL
    assert settings.REDIS_URL


def test_env_override_wins(clean_env):
    clean_env.setenv("JWT_SECRET", "rotated-secret")
    clean_env.setenv("REQUEST_TIMEOUT_MS", "250")
    settings = _settings()
    assert settings.JWT_SECRET == "rotated-secret"
    assert settings.REQUEST_TIMEOUT_MS == 250


@pytest.mark.parametrize("raw", ["1", "true", "True", "yes"])
def test_test_mode_coerces_truthy_strings(clean_env, raw):
    clean_env.setenv("TEST_MODE", raw)
    assert _settings().TEST_MODE is True


@pytest.mark.parametrize("raw", ["0", "false"])
def test_test_mode_coerces_falsy_strings(clean_env, raw):
    clean_env.setenv("TEST_MODE", raw)
    assert _settings().TEST_MODE is False


def test_get_settings_is_cached(clean_env):
    assert get_settings() is get_settings()
    get_settings.cache_clear()
    clean_env.setenv("CACHE_TTL_MS", "1234")
    assert get_settings().CACHE_TTL_MS == 1234
