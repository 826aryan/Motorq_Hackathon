"""A tiny FastAPI app with GET /health that checks the dependencies a service needs.

Each service calls create_health_app(deps=[...]) with only the stores it actually uses.
"""
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from shared.config import settings


def check_postgres() -> None:
    import psycopg
    with psycopg.connect(settings.postgres_dsn, connect_timeout=3) as conn:
        conn.execute("SELECT 1")


def check_timescale() -> None:
    import psycopg
    with psycopg.connect(settings.timescale_dsn, connect_timeout=3) as conn:
        conn.execute("SELECT 1")


def check_redis() -> None:
    import redis
    redis.Redis.from_url(settings.redis_url, socket_timeout=3).ping()


def check_kafka() -> None:
    from confluent_kafka.admin import AdminClient
    AdminClient({"bootstrap.servers": settings.kafka_bootstrap}).list_topics(timeout=3)


CHECKS: dict[str, Callable[[], None]] = {
    "postgres": check_postgres,
    "timescale": check_timescale,
    "redis": check_redis,
    "kafka": check_kafka,
}


def run_checks(deps: list[str], checks: dict[str, Callable[[], None]] = CHECKS) -> dict:
    results = {}
    for dep in deps:
        try:
            checks[dep]()
            results[dep] = "ok"
        except Exception as exc:  # noqa: BLE001 — report, don't crash: health must always answer
            results[dep] = f"error: {type(exc).__name__}"
    healthy = all(v == "ok" for v in results.values())
    return {"service": settings.service_name, "status": "ok" if healthy else "degraded", "deps": results}


def create_health_app(deps: list[str], extra_checks: dict[str, Callable[[], None]] | None = None) -> FastAPI:
    app = FastAPI(title=settings.service_name)
    checks = {**CHECKS, **(extra_checks or {})}

    @app.get("/health")
    def health():
        body = run_checks(deps, checks)
        return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)

    return app
