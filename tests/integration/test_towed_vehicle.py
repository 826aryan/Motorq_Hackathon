"""Integration (SPEC §10): real Kafka (Redpanda), Redis Stack and PostGIS in throwaway containers.

A scripted towed vehicle is sent as raw OEM B lines; the real pipeline and detectors loops run in threads.
Pass = a TOW_SUSPECTED alert row in PostgreSQL within 60 s of the first event.

    pytest tests/integration        (needs Docker; not part of the default quick run)
"""
import importlib
import os
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("testcontainers")
from testcontainers.community.kafka import RedpandaContainer
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from shared.geo import circle_polygon, offset_m
from simulator.encoders import Reading, encode_b

CENTER = (12.9716, 77.5946)
VID = "VH-000001"


@pytest.fixture(scope="module")
def stack():
    with RedpandaContainer("redpandadata/redpanda:v24.2.13") as kafka, \
            RedisContainer("redis/redis-stack-server:7.4.0-v1") as rds, \
            PostgresContainer("postgis/postgis:16-3.4", username="asset", password="test-only", dbname="asset") as pg:
        os.environ.update({
            "KAFKA_BOOTSTRAP": kafka.get_bootstrap_server(),
            "REDIS_URL": f"redis://{rds.get_container_host_ip()}:{rds.get_exposed_port(6379)}/0",
            "POSTGRES_HOST": pg.get_container_host_ip(), "POSTGRES_PORT": str(pg.get_exposed_port(5432)),
            "POSTGRES_USER": "asset", "POSTGRES_PASSWORD": "test-only", "POSTGRES_DB": "asset",
        })
        import shared.config
        importlib.reload(shared.config)             # settings are read from the environment at import
        _migrate_and_seed(shared.config.settings.postgres_dsn)
        _create_topics(kafka.get_bootstrap_server())
        yield shared.config.settings


def _migrate_and_seed(dsn: str) -> None:
    import psycopg
    with psycopg.connect(dsn) as conn:
        for sql in sorted(Path("db/migrations").glob("*.sql")):
            conn.execute(sql.read_text())
        lender = conn.execute("INSERT INTO lender (name) VALUES ('Lender Test') RETURNING lender_id").fetchone()[0]
        ring = circle_polygon(*CENTER, 6000)
        wkt = "POLYGON((" + ", ".join(f"{lon} {lat}" for lat, lon in [*ring, ring[0]]) + "))"
        fence = conn.execute("INSERT INTO geofence (name, area, lender_id) VALUES ('core', ST_GeogFromText(%s), %s) "
                             "RETURNING geofence_id", (wkt, lender)).fetchone()[0]
        vm = conn.execute("INSERT INTO vehicle_model (make, model, year) VALUES ('Test', 'Car', 2024) "
                          "RETURNING vehicle_model_id").fetchone()[0]
        conn.execute("INSERT INTO vehicle VALUES (%s, 'SIMVIN00000000001', '2024-01-01', %s)", (VID, vm))
        conn.execute("INSERT INTO vehicle_geofence VALUES (%s, %s)", (VID, fence))
        borrower = conn.execute("INSERT INTO borrower (name_hash, phone_hash) VALUES (repeat('a', 64), repeat('b', 64)) "
                                "RETURNING borrower_id").fetchone()[0]
        conn.execute("INSERT INTO loan (amount, start_date, status, days_past_due, lender_id, borrower_id, vehicle_id) "
                     "VALUES (500000, '2024-01-01', 'overdue', 45, %s, %s, %s)", (lender, borrower, VID))


def _create_topics(bootstrap: str) -> None:
    from confluent_kafka.admin import AdminClient, NewTopic
    admin = AdminClient({"bootstrap.servers": bootstrap})
    names = ["raw.telemetry", "normalized.events", "dead.letter", "late.events", "detector.signals"]
    for f in admin.create_topics([NewTopic(t, num_partitions=3, replication_factor=1) for t in names]).values():
        f.result(30)


def _start(module_path: str):
    mod = importlib.reload(importlib.import_module(module_path))
    runner = mod.Runner()
    t = threading.Thread(target=runner.run, daemon=True)
    t.start()
    return runner


def test_towed_vehicle_raises_tow_alert_within_60s(stack):
    import psycopg
    from confluent_kafka import Producer

    pipeline, detectors = _start("pipeline.runner"), _start("detectors.runner")
    producer = Producer({"bootstrap.servers": stack.kafka_bootstrap})
    t0 = time.time()
    try:
        # Parked, then towed north at ~25 km/h with the ignition off; one OEM B event every 10 s (sent fast).
        start = offset_m(*CENTER, -2000, 0)
        for seq in range(1, 26):
            moved = max(0, seq - 5) * 70
            lat, lon = offset_m(*start, moved, 0)
            r = Reading(VID, seq, t0 - 250 + seq * 10, lat, lon, 25.0 if moved else 0.0, 0.0, False, 4.1, 1000.0)
            line = encode_b(r).encode()
            producer.produce("raw.telemetry", key=VID, value=line)
            if seq % 5 == 0:
                producer.produce("raw.telemetry", key=VID, value=line)      # chaos: a duplicate
        producer.flush(10)

        deadline = t0 + 60
        found = None
        while time.time() < deadline and not found:
            with psycopg.connect(stack.postgres_dsn) as conn:
                found = conn.execute("""SELECT a.alert_id FROM alert a JOIN signal_type s USING (signal_type_id)
                                        WHERE a.vehicle_id = %s AND s.code = 'TOW_SUSPECTED'""", (VID,)).fetchone()
            time.sleep(1)
        latency = time.time() - t0
        assert found, "no TOW_SUSPECTED alert within 60 s"
        assert pipeline.pipeline.counts["duplicates_dropped"] >= 5
        print(f"TOW_SUSPECTED alert after {latency:.1f} s")
    finally:
        pipeline.running = detectors.running = False
