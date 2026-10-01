"""detectors service: runs the detector loop in a background thread; /health and /stats over HTTP."""
import atexit
import logging
import threading

from detectors.runner import Runner
from shared.health import create_health_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

runner = Runner()
thread = threading.Thread(target=runner.run, name="detectors-loop", daemon=True)
thread.start()


@atexit.register
def _stop() -> None:
    runner.running = False
    thread.join(timeout=15)


def _loop_alive() -> None:
    if not thread.is_alive():
        raise RuntimeError("detectors loop stopped")


app = create_health_app(deps=["kafka", "redis", "postgres", "loop"], extra_checks={"loop": _loop_alive})


@app.get("/stats")
def stats():
    e = runner.engine
    return {"vehicles_in_window": len(e.anomaly.windows), "vehicles_with_signals": len(e.risk.active),
            "open_cases": len(e.risk.open_cases), "exact_polygon_checks": e.geofence.index.exact_checks,
            "event_clock": e.anomaly.clock}
