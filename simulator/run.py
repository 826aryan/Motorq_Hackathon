"""Simulator entrypoint: starts N worker processes, and serves /health and /stats on port 8000."""
import multiprocessing as mp
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
import yaml

from shared.health import create_health_app
from simulator.worker import run_worker

COUNTERS = ("emitted", "sent", "send_errors")


def sim_start(clock_cfg: dict, real0: float) -> float:
    """Sim epoch at launch. Simulated time is real time shifted by a fixed offset, so it keeps moving
    forward across restarts (a restart must never make device clocks jump back).

    time_scale != 1 speeds the day up from local midnight; that stays consistent across restarts
    on the same day.
    """
    tz = timezone(timedelta(hours=clock_cfg["timezone_offset_h"]))
    midnight = datetime.fromtimestamp(real0, tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return midnight + (real0 - midnight) * clock_cfg["time_scale"] + clock_cfg["offset_h"] * 3600


def demo_offset_s(clock_cfg: dict, base_sim: float, prev_offset_s: float) -> float:
    """Demo mode: extra seconds added to sim time so the demo starts at rush hour (clock.demo_start_hour).

    The offset only ever grows (it is saved between launches), so device clocks never jump back:
    a restart inside the demo window keeps the previous offset; otherwise jump forward to the next start hour.
    """
    hour = clock_cfg.get("demo_start_hour")
    if hour is None:
        return prev_offset_s
    tz = timezone(timedelta(hours=clock_cfg["timezone_offset_h"]))
    now = datetime.fromtimestamp(base_sim + prev_offset_s, tz)
    start = now.replace(hour=int(hour), minute=0, second=0, microsecond=0)
    if start <= now < start + timedelta(hours=clock_cfg.get("demo_window_h", 3)):
        return prev_offset_s
    if start <= now:
        start += timedelta(days=1)
    return prev_offset_s + (start - now).total_seconds()


def main() -> None:
    config_path = os.environ.get("SIM_CONFIG", "simulator/config.yaml")
    cfg = yaml.safe_load(Path(config_path).read_text())
    for env, key in (("SIM_VEHICLES", "vehicles"), ("SIM_WORKERS", "workers")):   # load-test overrides
        if os.environ.get(env):
            cfg["fleet"][key] = int(os.environ[env])
    ctx = mp.get_context("spawn")
    counters = {name: ctx.Value("q", 0) for name in COUNTERS}
    real0 = time.time()
    sim0 = sim_start(cfg["clock"], real0)
    state = Path(os.environ.get("SIM_STATE_DIR", "/app/state")) / "demo_offset_s"
    prev = float(state.read_text()) if state.exists() else 0.0
    offset = demo_offset_s(cfg["clock"], sim0, prev)
    if offset != prev:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(str(offset))
    sim0 += offset

    f = cfg["fleet"]
    active = ctx.Value("q", min(int(os.environ.get("SIM_ACTIVE") or f.get("active_vehicles", f["vehicles"])), f["vehicles"]))
    procs = [ctx.Process(target=run_worker, args=(w, cfg, config_path, sim0, real0, counters, active), daemon=True)
             for w in range(cfg["fleet"]["workers"])]
    for p in procs:
        p.start()

    def workers_alive() -> None:
        dead = [i for i, p in enumerate(procs) if not p.is_alive()]
        if dead:
            raise RuntimeError(f"workers down: {dead}")

    app = create_health_app(deps=["workers"], extra_checks={"workers": workers_alive})

    @app.get("/stats")
    def stats():
        elapsed = time.time() - real0
        values = {name: c.value for name, c in counters.items()}
        return {"vehicles": cfg["fleet"]["vehicles"], "uptime_s": round(elapsed, 1),
                "active_vehicles": active.value, "presets": f.get("active_presets", []),
                "clock_offset_s": round(sim0 - real0, 1),        # device time - wall time (demo mode)
                "events_per_s": round(values["emitted"] / elapsed, 1) if elapsed else 0.0, **values}

    @app.post("/control")
    def control(body: dict):
        """Load control: how many of the loaded vehicles report (0..fleet size). Takes effect within one interval."""
        active.value = max(0, min(int(body["active_vehicles"]), f["vehicles"]))
        return {"active_vehicles": active.value}

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
