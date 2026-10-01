"""Ingestion gateway: POST /ingest with newline-separated raw OEM payloads -> Kafka raw.telemetry.

The gateway does not parse (that is the pipeline's job). It only pulls out the vehicle ID with a
cheap regex so each message is keyed by vehicle_id and lands on that vehicle's partition.
Payloads with no recognisable ID are still forwarded (unkeyed); the parser sends them to dead.letter.
"""
import re
import threading

from confluent_kafka import Producer
from fastapi import Request

from shared.config import settings
from shared.health import create_health_app

RAW_TOPIC = "raw.telemetry"
VEHICLE_ID = re.compile(rb"VH-\d{6}")

producer = Producer({
    "bootstrap.servers": settings.kafka_bootstrap,
    "linger.ms": 20,
    "compression.type": "lz4",
    "queue.buffering.max.messages": 500_000,
    "enable.idempotence": True,
})
stats = {"received": 0, "unkeyed": 0, "produce_errors": 0}
_lock = threading.Lock()


def _on_delivery(err, _msg) -> None:
    if err is not None:
        with _lock:
            stats["produce_errors"] += 1


def _poll_forever() -> None:
    while True:
        producer.poll(0.1)   # serve delivery callbacks


threading.Thread(target=_poll_forever, daemon=True).start()

app = create_health_app(deps=["kafka"])


@app.post("/ingest", status_code=202)
async def ingest(request: Request):
    body = await request.body()
    lines = [ln for ln in body.split(b"\n") if ln]
    unkeyed = 0
    for line in lines:
        m = VEHICLE_ID.search(line)
        if m is None:
            unkeyed += 1
        while True:
            try:
                producer.produce(RAW_TOPIC, value=line, key=m.group() if m else None, on_delivery=_on_delivery)
                break
            except BufferError:          # local queue full during a burst: let it drain, then retry
                producer.poll(0.05)
    with _lock:
        stats["received"] += len(lines)
        stats["unkeyed"] += unkeyed
    return {"accepted": len(lines)}


@app.get("/stats")
def get_stats():
    return {**stats, "queued": len(producer)}
