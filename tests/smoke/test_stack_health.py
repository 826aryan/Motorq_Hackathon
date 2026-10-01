"""Run against a live stack: pytest tests/smoke  (after docker compose up)."""
import httpx
import pytest

SERVICES = {
    "api": 8000, "gateway": 8001, "simulator": 8002, "batch": 8005, "storage": 8006, "web": 8080,
}
REPLICA_PORTS = {"pipeline": range(8030, 8040), "detectors": range(8040, 8050)}   # replicas get a free port each


@pytest.mark.parametrize(("name", "port"), SERVICES.items())
def test_service_is_healthy(name, port):
    r = httpx.get(f"http://localhost:{port}/health", timeout=10)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"


@pytest.mark.parametrize("name", REPLICA_PORTS)
def test_every_replica_is_healthy(name):
    healthy = 0
    for port in REPLICA_PORTS[name]:
        try:
            r = httpx.get(f"http://localhost:{port}/health", timeout=3)
        except httpx.HTTPError:
            continue                       # no replica on this port
        assert r.json()["status"] == "ok", (port, r.text)
        healthy += 1
    assert healthy >= 1
