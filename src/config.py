"""Typed settings, read once from the environment.

Every later phase reads ``CACHE_TTL_MS``, ``REQUEST_TIMEOUT_MS`` and
``CONTROL_PLANE_TTL_MS``; a typo in a raw ``os.getenv`` is a silent default, so
the process reads its configuration through exactly one typed object.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """The environment variables documented in ``.env.example``."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # >= 32 bytes: below that, PyJWT raises InsecureKeyLengthWarning for
    # HS256 (RFC 7518 §3.2). Overridden per environment via .env.
    JWT_SECRET: str = "dev-only-signing-key-not-for-production"
    JWT_AUDIENCE: str = "ema-universal-sql"
    DATABASE_URL: str = "postgresql://postgres:postgres@postgres:5432/universal_sql"
    REDIS_URL: str = "redis://redis:6379/0"
    REQUEST_TIMEOUT_MS: int = 5000
    CONTROL_PLANE_TTL_MS: int = 30000
    CACHE_TTL_MS: int = 60000
    TEST_MODE: bool = False

    # Scopes minted into a demo token (RFC 8693 §4.2: space-delimited).
    # The gateway's coarse L2 check requires `query:execute`.
    DEFAULT_SCOPES: str = "query:execute"

    # Tracing sink. ADR-016 keeps this backend-free; "file" writes JSONL that
    # Phase 4 renders the waterfall from. "console" is for local debugging and
    # "none" is for the k6 run, where exporting is measurement overhead.
    OTEL_EXPORTER: Literal["file", "console", "none"] = "file"
    OTEL_TRACE_FILE: str = "traces/spans.jsonl"

    LOG_LEVEL: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings.

    Cached, so the environment is read once. Tests override by calling
    ``get_settings.cache_clear()`` after patching the environment.
    """
    return Settings()


settings = get_settings()
