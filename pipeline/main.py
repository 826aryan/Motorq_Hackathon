"""pipeline service: runs the Kafka cleaning loop in a background thread; /health and /stats over HTTP."""
import atexit
import logging
import threading

from pipeline.runner import Runner
from shared.health import create_health_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

runner = Runner()
thread = threading.Thread(target=runner.run, name="pipeline-loop", daemon=True)
thread.start()


@atexit.register
def _stop() -> None:
    # On container stop: finish the current batch, publish held events, commit offsets, close cleanly.
    runner.running = False
    thread.join(timeout=15)


def _loop_alive() -> None:
    if not thread.is_alive():
        raise RuntimeError("pipeline loop stopped")


app = create_health_app(deps=["kafka", "redis", "loop"], extra_checks={"loop": _loop_alive})


@app.get("/stats")
def stats():
    p = runner.pipeline
    counts = {}
    for _ in range(3):   # the loop thread may be updating counters; retry the copy
        try:
            counts = dict(p.counts)
            break
        except RuntimeError:
            continue
    return {**counts, "held_in_reorder": p.reorder.held(),
            "gps_snapped": p.noise.snapped, "vehicles_tracked": len(p.reorder.v),
            "sessions_restarted": p.reorder.sessions_restarted}
