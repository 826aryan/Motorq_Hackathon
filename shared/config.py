"""Connection settings read from environment variables (filled from .env by docker compose).

No secrets are hardcoded here: defaults only point at local container hostnames.
"""
import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    service_name: str = _env("SERVICE_NAME", "unknown")

    postgres_dsn: str = (
        f"postgresql://{_env('POSTGRES_USER', 'asset')}:{_env('POSTGRES_PASSWORD')}"
        f"@{_env('POSTGRES_HOST', 'postgres')}:{_env('POSTGRES_PORT', '5432')}/{_env('POSTGRES_DB', 'asset_recovery')}"
    )
    timescale_dsn: str = (
        f"postgresql://{_env('TIMESCALE_USER', 'asset')}:{_env('TIMESCALE_PASSWORD')}"
        f"@{_env('TIMESCALE_HOST', 'timescaledb')}:{_env('TIMESCALE_PORT', '5432')}/{_env('TIMESCALE_DB', 'telemetry')}"
    )
    redis_url: str = _env("REDIS_URL", "redis://redis:6379/0")
    kafka_bootstrap: str = _env("KAFKA_BOOTSTRAP", "redpanda:9092")


settings = Settings()
