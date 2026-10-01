"""Fast offline end-to-end run: real simulator worker -> real parser/dedup/reorder/noise -> real detectors,
on a simulated clock (no Kafka, no HTTP, chaos off). Shows which persona produced which signal.

    python -m tests.tools.scenario --hours 2.5 --vehicles 1000
"""
import argparse
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from detectors.engine import DetectorEngine, EngineState
from pipeline.core import Pipeline
from pipeline.dedup import Deduper
from pipeline.noise import NoiseFilter
from pipeline.reorder import ReorderBuffer
from shared.detect.convoy import ConvoyDetector, LiveState
from shared.detect.risk import Loan
from shared.geo import circle_polygon
from shared.roadgraph import RoadGraph
from simulator import worker as sim_worker


class ExactBloom:
    def __init__(self):
        self.s: dict[str, set] = {}

    def reserve(self, key, *_):
        self.s.setdefault(key, set())

    def mexists(self, key, items):
        return [i in self.s.get(key, ()) for i in items]

    def madd(self, key, items):
        out = []
        for i in items:
            out.append(i not in self.s.setdefault(key, set()))
            self.s[key].add(i)
        return out


class _Counter:
    def __init__(self):
        self.value = 0

    def get_lock(self):
        import contextlib
        return contextlib.nullcontext()


def run(hours: float, vehicles: int, chaos_off: bool = True) -> dict:
    sim_cfg = yaml.safe_load(Path("simulator/config.yaml").read_text())
    sim_cfg["fleet"]["vehicles"], sim_cfg["fleet"]["workers"] = vehicles, 1
    if chaos_off:
        sim_cfg["chaos"] = {k: (0 if isinstance(v, int | float) else v) for k, v in sim_cfg["chaos"].items()}
        sim_cfg["chaos"]["gps_jump_m"] = [1000, 5000]
    cfg_file = Path(tempfile.mkdtemp()) / "sim.yaml"
    cfg_file.write_text(yaml.safe_dump(sim_cfg))

    t = [time.time()]
    counters = {k: _Counter() for k in ("emitted", "sent", "send_errors")}
    w = sim_worker.Worker(0, sim_cfg, str(cfg_file), t[0], t[0], counters)
    w.clock.now = lambda: t[0]                       # drive the worker on our own clock
    sim_worker.time.time = lambda: t[0]              # chaos release times use the same clock

    det_cfg = yaml.safe_load(Path("detectors/config.yaml").read_text())
    pipe_cfg = yaml.safe_load(Path("pipeline/config.yaml").read_text())
    centre = sim_cfg["graph"]["center"]
    state = EngineState({1: circle_polygon(*centre, 6000)}, {v.s.vehicle_id: [1] for v in w.vehicles},
                        {v.s.vehicle_id: Loan(i, 1, 45) for i, v in enumerate(w.vehicles)})
    engine = DetectorEngine(det_cfg, state, RoadGraph.load(sim_cfg["graph"]["path"], 0.6))
    pipe = Pipeline(Deduper(ExactBloom(), **pipe_cfg["dedup"]), ReorderBuffer(**pipe_cfg["reorder"]),
                    NoiseFilter(**pipe_cfg["noise"]))
    convoy = ConvoyDetector(det_cfg["convoy"])
    persona = {v.s.vehicle_id: v.s.persona for v in w.vehicles}

    signals: dict[str, Counter] = defaultdict(Counter)
    latest: dict[str, tuple] = {}
    convoy_log = []
    end, next_convoy = t[0] + hours * 3600, t[0] + det_cfg["convoy"]["every_s"]
    while t[0] < end:
        t[0] += 1.0
        w._emit_due()
        batch, w.outbox = [(p.encode(), t[0]) for p in w.outbox], []
        out = pipe.process(batch, now=t[0])
        for e in out.normalized:
            for s in engine.on_event(e):
                signals[persona[e.vehicle_id]][s.signal.code] += 1
            if "suspect_gps" not in e.flags:
                latest[e.vehicle_id] = (e.device_ts, LiveState(e.vehicle_id, e.lat, e.lon, e.speed_kmh, e.heading_deg, e.geohash))
        for s in engine.tick():
            signals[persona[s.signal.vehicle_id]][s.signal.code] += 1
        if t[0] >= next_convoy:
            next_convoy += det_cfg["convoy"]["every_s"]
            fresh = [st for ts, st in latest.values() if t[0] - ts <= det_cfg["convoy"]["fresh_s"]]
            groups = convoy.run(fresh)
            members = sorted(v for v, p in persona.items() if p == "convoy")
            edges = {e: n for e, n in convoy.edges.items() if e[0] in members and e[1] in members}
            convoy_log.append((round((t[0] - end) / 60 + hours * 60), len(edges), max(edges.values(), default=0),
                               [sorted(g) for g in groups]))
    return {"signals": {p: dict(c) for p, c in signals.items()}, "convoy_runs": convoy_log,
            "pipeline": dict(pipe.counts)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=2.5)
    ap.add_argument("--vehicles", type=int, default=1000)
    ap.add_argument("--chaos", action="store_true", help="keep the simulator chaos settings on")
    a = ap.parse_args()
    started = time.time()
    r = run(a.hours, a.vehicles, chaos_off=not a.chaos)
    print("signals by persona:")
    for p, c in sorted(r["signals"].items()):
        print(f"  {p:11s} {c}")
    print("convoy runs (minute, member edges seen, best streak, groups):")
    for row in r["convoy_runs"]:
        print("  ", row)
    print("pipeline:", r["pipeline"])
    print(f"({time.time() - started:.0f} s)")
