"""Load test (SPEC §10): full stack at N vehicles; records throughput, Kafka lag and latencies.

    python -m tests.load.run_load --vehicles 100000 --workers 6 --warmup 180 --measure 300

Restarts the simulator with the given fleet (SIM_VEHICLES / SIM_WORKERS), then samples every 10 s.
Writes tests/load/results.json and prints a Markdown table for the README. Restores the simulator after.
"""
import argparse
import json
import os
import statistics
import subprocess
import threading
import time
from pathlib import Path

import httpx
import redis
from confluent_kafka import Consumer, TopicPartition

COMPOSE = ["docker", "compose", "-f", "infra/docker-compose.yml", "--env-file", ".env"]
ENV = dict(line.split("=", 1) for line in Path(".env").read_text().splitlines() if "=" in line and not line.startswith("#"))
REDIS = f"redis://localhost:{ENV.get('REDIS_HOST_PORT', '6379')}/0"
KAFKA = "localhost:19092"
GROUPS = {"pipeline": "raw.telemetry", "detectors": "normalized.events", "storage": "normalized.events"}


def set_active(n: int) -> None:
    """Live load control (no restart): how many of the simulator's loaded vehicles report."""
    httpx.post("http://localhost:8002/control", json={"active_vehicles": n}, timeout=20).raise_for_status()


def restart_simulator(vehicles: int | None, workers: int | None) -> None:
    env = {**os.environ, "SIM_VEHICLES": str(vehicles or ""), "SIM_WORKERS": str(workers or "")}
    subprocess.run([*COMPOSE, "up", "-d", "--no-deps", "--force-recreate", "simulator"], env=env, check=True,
                   capture_output=True)


def lag(consumers: dict[str, Consumer]) -> dict[str, int]:
    out = {}
    for group, topic in GROUPS.items():
        c = consumers[group]
        parts = [TopicPartition(topic, p) for p in c.list_topics(topic, timeout=5).topics[topic].partitions]
        out[group] = sum(max(0, c.get_watermark_offsets(tp, timeout=5, cached=False)[1] - tp.offset)
                         for tp in c.committed(parts, timeout=5) if tp.offset >= 0)
    return out


def pipeline_latency(sample: int = 3000) -> list[float]:
    """normalized.events broker timestamp - ingest_ts (gateway time) for the newest messages."""
    c = Consumer({"bootstrap.servers": KAFKA, "group.id": "load-latency", "enable.auto.commit": False})
    t = "normalized.events"
    tps = []
    for p in c.list_topics(t, timeout=10).topics[t].partitions:
        lo, hi = c.get_watermark_offsets(TopicPartition(t, p), timeout=10)
        tps.append(TopicPartition(t, p, max(lo, hi - sample // 12)))
    c.assign(tps)
    out = []
    for _ in range(5):
        for m in c.consume(5000, timeout=2):
            if not m.error():
                out.append(m.timestamp()[1] / 1000 - json.loads(m.value())["ingest_ts"])
    c.close()
    return out


def sim_stats() -> dict | None:
    """The simulator is CPU-bound at 100k vehicles, so its /stats can be slow: retry, then give up on this sample."""
    for _ in range(3):
        try:
            return httpx.get("http://localhost:8002/stats", timeout=20).json()
        except httpx.HTTPError:
            time.sleep(2)
    return None


def pct(values: list[float], q: float) -> float:
    return round(statistics.quantiles(values, n=100)[int(q) - 1], 1) if len(values) > 2 else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicles", type=int, default=100_000)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--warmup", type=int, default=180)
    ap.add_argument("--measure", type=int, default=300)
    ap.add_argument("--no-restart", action="store_true",
                    help="the simulator is already running at --vehicles (e.g. where host docker compose is unavailable)")
    a = ap.parse_args()

    r = redis.Redis.from_url(REDIS, decode_responses=True)
    alert_latency: list[float] = []

    def listen() -> None:                              # event time -> alert published on Redis Pub/Sub
        ps = r.pubsub()
        ps.subscribe("alerts")
        for msg in ps.listen():
            if msg["type"] == "message":
                # alert ts is device time; demo mode runs device clocks ahead of the wall clock
                alert_latency.append(time.time() + clock_offset[0] - json.loads(msg["data"])["ts"])

    print(f"restarting simulator with {a.vehicles} vehicles / {a.workers} workers ...")
    if not a.no_restart:
        restart_simulator(a.vehicles, a.workers)
    before = (sim_stats() or {}).get("active_vehicles")
    set_active(a.vehicles)                             # the whole requested fleet reports during the test
    time.sleep(a.warmup)
    clock_offset = [(sim_stats() or {}).get("clock_offset_s", 0.0)]
    threading.Thread(target=listen, daemon=True).start()
    consumers = {g: Consumer({"bootstrap.servers": KAFKA, "group.id": g, "enable.auto.commit": False}) for g in GROUPS}

    samples = []
    start = time.time()
    while time.time() - start < a.measure:
        sim = sim_stats()
        if sim is None:
            continue                                   # skip this sample; rates use the first and last good ones
        samples.append({"t": time.time(), "sim": sim, "pipe": r.hgetall("stats:pipeline"),
                        "det": r.hgetall("stats:detectors"), "lag": lag(consumers)})
        time.sleep(10)
    first, last = samples[0], samples[-1]
    dt = last["t"] - first["t"]

    def rate(get) -> float:
        return round((get(last) - get(first)) / dt)

    lat = pipeline_latency()
    lags = {g: [s["lag"][g] for s in samples] for g in GROUPS}
    result = {
        "vehicles": a.vehicles, "window_s": round(dt),
        "events_per_s_generated": rate(lambda s: s["sim"]["emitted"]),
        "events_per_s_sent_incl_duplicates": rate(lambda s: s["sim"]["sent"]),
        "events_per_s_pipeline_in": rate(lambda s: int(s["pipe"].get("in", 0))),
        "events_per_s_cleaned": rate(lambda s: int(s["pipe"].get("normalized", 0))),
        "events_per_s_detectors": rate(lambda s: int(s["det"].get("events", 0))),
        "send_errors": last["sim"]["send_errors"],
        "kafka_lag_max": {g: max(v) for g, v in lags.items()},
        "kafka_lag_end": {g: v[-1] for g, v in lags.items()},
        "gateway_to_clean_latency_s": {"p50": pct(lat, 50), "p90": pct(lat, 90), "p99": pct(lat, 99)},
        "alerts_seen": len(alert_latency),
        "event_to_alert_latency_s": {"p50": pct(alert_latency, 50), "p90": pct(alert_latency, 90)},
    }
    Path("tests/load/results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    print("\n| metric | value |\n| --- | --- |")
    for k, v in result.items():
        print(f"| {k.replace('_', ' ')} | {v} |")

    print("restoring simulator to its configured fleet ...")
    if before is not None:
        set_active(before)
    if not a.no_restart:
        restart_simulator(None, None)


if __name__ == "__main__":
    main()
