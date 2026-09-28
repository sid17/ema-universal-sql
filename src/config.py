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
    # 300s, comfortably longer than any max_staleness_ms the demo uses.
    # TTL is a property of the WRITE, staleness a property of the READ
    # (ADR-023): at 60s this and the demo's staleness knob were the same
    # number, so a cache hit depended on which boundary fell first.
    CACHE_TTL_MS: int = 300000
    TEST_MODE: bool = False

    # Scopes minted into a demo token (RFC 8693 §4.2: space-delimited).
    # The gateway's coarse L2 check requires `query:execute`.
    DEFAULT_SCOPES: str = "query:execute"

    # How much of each mock adapter's `simulated_latency_ms` to actually sleep.
    # DEFAULT 0.0 — OFF (ADR-038). The unit suite runs on the commit hook and
    # must stay instant, and a sleep there would buy nothing: what the tests
    # assert is that the delay lands on the live path and not on a cache hit,
    # which a scale of 1.0 passed explicitly to one adapter proves just as well.
    # docker-compose sets this to 1.0 so the running stack, the trace waterfall
    # and `make demo` all show a source that costs what a remote source costs.
    MOCK_LATENCY_SCALE: float = 0.0

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
