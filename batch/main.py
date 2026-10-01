"""batch service: runs the DuckDB jobs on schedule (nightly 02:00, report 06:00 local) and on demand.

    POST /jobs/{name}?date=YYYY-MM-DD      run one job now (export | baseline | rescore | convoy | report)
    POST /jobs/nightly?date=...            the whole nightly chain, e.g. for a demo
"""
import logging
import threading
import time
from datetime import date, datetime

from fastapi import HTTPException

from batch import jobs
from shared.health import create_health_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("batch")
NIGHTLY = ["export", "baseline", "rescore", "convoy"]
_lock = threading.Lock()                     # one job at a time
history: list[dict] = []


def run_chain(names: list[str], d: date | None) -> list[dict]:
    with _lock:
        out = []
        for name in names:
            try:
                out.append({"job": name, **jobs.run(name, d)})
            except Exception as exc:
                log.exception("job %s failed", name)
                out.append({"job": name, "error": f"{type(exc).__name__}: {exc}"})
        history.extend(out)
        del history[:-50]
        return out


def scheduler() -> None:
    fired: set[tuple[str, str]] = set()
    while True:
        now = datetime.now(jobs.TZ)
        hhmm, today = now.strftime("%H:%M"), now.date().isoformat()
        for slot, names in ((jobs.CFG["schedule"]["nightly"], NIGHTLY), (jobs.CFG["schedule"]["report"], ["report"])):
            if hhmm == slot and (slot, today) not in fired:
                fired.add((slot, today))
                threading.Thread(target=run_chain, args=(names, None), daemon=True).start()
        time.sleep(20)


threading.Thread(target=scheduler, daemon=True).start()
app = create_health_app(deps=["postgres", "timescale", "redis"])


@app.post("/jobs/{name}")
def run_job(name: str, date: date | None = None):
    names = NIGHTLY if name == "nightly" else [name]
    if any(n not in jobs.JOBS for n in names):
        raise HTTPException(404, f"unknown job; one of {sorted(jobs.JOBS)} or nightly")
    return {"results": run_chain(names, date)}


@app.get("/jobs")
def job_history():
    return {"history": history}
