"""storage service: normalized.events + late.events -> TimescaleDB hypertable `telemetry`.

Batched COPY into a temp table, then INSERT ... ON CONFLICT DO NOTHING, so a re-delivered batch never
duplicates rows. Late events get the flag `late` so the nightly re-scorer can find them.
"""
import atexit
import io
import json
import logging
import threading

import psycopg
from confluent_kafka import Consumer

from shared.config import settings
from shared.health import create_health_app
from storage.rows import COLUMNS, to_row

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("storage")
TOPICS = ["normalized.events", "late.events"]
stats = {"rows_written": 0, "batches": 0}


class Writer:
    def __init__(self):
        self.running = True
        self.consumer = Consumer({"bootstrap.servers": settings.kafka_bootstrap, "group.id": "storage",
                                  "auto.offset.reset": "earliest", "enable.auto.commit": False})

    def write(self, conn, rows: list[str]) -> None:
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE IF NOT EXISTS t_in (LIKE telemetry INCLUDING DEFAULTS) ON COMMIT DELETE ROWS")
            with cur.copy(f"COPY t_in ({', '.join(COLUMNS)}) FROM STDIN") as cp:
                cp.write(io.StringIO("".join(rows)).read())
            cur.execute("INSERT INTO telemetry SELECT * FROM t_in ON CONFLICT DO NOTHING")
        conn.commit()

    def run(self) -> None:
        self.consumer.subscribe(TOPICS)
        with psycopg.connect(settings.timescale_dsn) as conn:
            while self.running:
                msgs = [m for m in self.consumer.consume(5000, timeout=1.0) if not m.error()]
                if not msgs:
                    continue
                rows = [to_row(json.loads(m.value()), m.topic() == "late.events") for m in msgs]
                self.write(conn, rows)
                self.consumer.commit(asynchronous=False)   # commit only after the rows are safely stored
                stats["rows_written"] += len(rows)
                stats["batches"] += 1
        self.consumer.close()


writer = Writer()
thread = threading.Thread(target=writer.run, daemon=True)
thread.start()


@atexit.register
def _stop() -> None:
    writer.running = False
    thread.join(timeout=15)


def _loop_alive() -> None:
    if not thread.is_alive():
        raise RuntimeError("storage loop stopped")


app = create_health_app(deps=["kafka", "timescale", "loop"], extra_checks={"loop": _loop_alive})


@app.get("/stats")
def get_stats():
    return stats
