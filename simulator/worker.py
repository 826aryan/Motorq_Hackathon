"""One simulator worker process: drives its share of the fleet and sends batches to the gateway.

Loop (every ~50 ms): pop vehicles whose next emission is due (min-heap by time) -> reading ->
GPS noise -> OEM encode -> chaos damage -> hold delayed copies -> POST ready payloads in batches.
"""
import asyncio
import heapq
import logging
import math
import time

import httpx
import numpy as np

from shared.fleet import build_fleet, core_nodes, distance_from, vehicle_id
from shared.roadgraph import RoadGraph
from simulator.chaos import Chaos
from simulator.encoders import ENCODERS, encode_c
from simulator.vehicle import Planner, Spec, Trip, Vehicle

log = logging.getLogger("simulator.worker")
TICK_S = 0.05


class SimClock:
    def __init__(self, sim0: float, real0: float, scale: float):
        self.sim0, self.real0, self.scale = sim0, real0, scale

    def now(self) -> float:
        return self.sim0 + (time.time() - self.real0) * self.scale

    def sim_at_real_offset(self, real_offset_s: float) -> float:
        return self.sim0 + real_offset_s * self.scale


def run_worker(worker_id: int, cfg: dict, config_path: str, sim0: float, real0: float, counters: dict,
               active=None) -> None:
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s worker-{worker_id} %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(Worker(worker_id, cfg, config_path, sim0, real0, counters, active).run())


class Worker:
    def __init__(self, worker_id, cfg, config_path, sim0, real0, counters, active=None):
        self.id = worker_id
        self.cfg = cfg
        self.counters = counters
        # Shared with the main process: vehicles with idx >= active.value stay silent (load control from the UI).
        self.active = active
        self.idle: set[int] = set()
        self.clock = SimClock(sim0, real0, cfg["clock"]["time_scale"])
        self.tz_s = cfg["clock"]["timezone_offset_h"] * 3600
        # The fleet (who, where, which OEM) is fixed by the seed; timing randomness also mixes in the
        # launch time, so two runs never emit identical events (same ts + seq = same event_id -> deduped).
        run = int(real0 * 1000)
        self.rng = np.random.default_rng([cfg["seed"], worker_id, run])
        self.chaos = Chaos(config_path, np.random.default_rng([cfg["seed"], worker_id, run, 1]))

        graph = RoadGraph.load(cfg["graph"]["path"], cfg["graph"]["traffic_speed_factor"])
        fleet = build_fleet(cfg, graph)
        self.planner = Planner(graph, self.rng)
        center = cfg["graph"]["center"]
        self.planner.core_nodes = core_nodes(graph, center, cfg["fleet"]["home_radius_m"])
        self.planner.outer_nodes = np.flatnonzero(
            distance_from(graph, center) >= cfg["personas"]["abscond_min_from_center_m"])
        now = self.clock.now()
        self.vehicles: list[Vehicle] = []
        self.convoys: dict[int, list[Vehicle]] = {}
        for i in np.flatnonzero(fleet.worker == worker_id):
            inc = fleet.incident_real_s[i]
            spec = Spec(int(i), vehicle_id(int(i)), str(fleet.oem[i]), str(fleet.persona[i]),
                        int(fleet.home[i]), int(fleet.work[i]), float(fleet.emit_interval_s[i]),
                        float(fleet.depart_s[i]), float(fleet.return_s[i]), int(fleet.convoy_group[i]),
                        math.inf if math.isnan(inc) else self.clock.sim_at_real_offset(float(inc)))
            v = Vehicle(spec, self.planner, cfg, self.tz_s, now)
            self.vehicles.append(v)
            if spec.convoy_group >= 0:
                self.convoys.setdefault(spec.convoy_group, []).append(v)

        # Spread first emissions over one interval so we don't start with a thundering herd.
        self.due = [(now + self.rng.uniform(0, v.s.emit_interval_s), k) for k, v in enumerate(self.vehicles)]
        heapq.heapify(self.due)
        self.convoy_pending = {g: m[0].s.incident_t for g, m in self.convoys.items()}
        self.held: list[tuple[float, str]] = []     # delayed (out-of-order) copies
        self.outbox: list[str] = []
        log.info("started: %d vehicles, %d convoys", len(self.vehicles), len(self.convoys))

    def _start_convoys(self, t: float) -> None:
        for g, when in list(self.convoy_pending.items()):
            if t < when:
                continue
            del self.convoy_pending[g]
            members = self.convoys[g]
            p = self.cfg["personas"]
            nodes = self.planner.multi_leg(members[0]._current_node(), p["convoy_legs"], 2000, 5000)
            if not nodes:
                continue
            trip = Trip(self.planner.graph, nodes, t)
            for rank, v in enumerate(members):          # followers trail the leader by a few seconds
                v.incident_done = True
                v.trip = trip.shifted(rank * 8.0)
            log.info("convoy %d started with %d vehicles (%.1f km)", g, len(members), trip.length_m / 1000)

    def _emit_due(self) -> None:
        now = self.clock.now()
        self._start_convoys(now)
        tz_h = self.cfg["clock"]["timezone_offset_h"]
        rate = self.chaos.send_rate_multiplier()
        real_now = time.time()
        while self.due and self.due[0][0] <= now:
            t, k = heapq.heappop(self.due)
            v = self.vehicles[k]
            heapq.heappush(self.due, (t + v.s.emit_interval_s / rate, k))
            if self.active is not None and v.s.idx >= self.active.value:
                if k not in self.idle:                 # switched off: park where it is, stay silent
                    self.idle.add(k)
                    v.pause(t)
                continue
            if k in self.idle:                         # switched back on: continue from the same spot
                self.idle.discard(k)
                v.resume(t)
            r = v.reading(t)
            if r is None:
                continue
            r = self.chaos.add_gps_noise(r)
            payload = encode_c(r, tz_h) if v.s.oem == "C" else ENCODERS[v.s.oem](r)
            self._count("emitted")
            for release, copy in self.chaos.damage(payload, v.s.oem, real_now):
                if release > real_now:
                    heapq.heappush(self.held, (release, copy))
                else:
                    self.outbox.append(copy)
        while self.held and self.held[0][0] <= real_now:
            self.outbox.append(heapq.heappop(self.held)[1])

    def _count(self, name: str, n: int = 1) -> None:
        c = self.counters[name]
        with c.get_lock():
            c.value += n

    async def run(self) -> None:
        gw = self.cfg["gateway"]
        sem = asyncio.Semaphore(4)
        last_flush = time.monotonic()
        async with httpx.AsyncClient(timeout=10) as client:
            async def send(batch: list[str]) -> None:
                async with sem:
                    try:
                        resp = await client.post(gw["url"], content="\n".join(batch).encode(),
                                                 headers={"content-type": "text/plain"})
                        resp.raise_for_status()
                        self._count("sent", len(batch))
                    except httpx.HTTPError as exc:
                        self._count("send_errors", len(batch))
                        log.warning("send failed (%d events dropped): %s", len(batch), type(exc).__name__)

            while True:
                self.chaos.reload()
                self._emit_due()
                flush_due = time.monotonic() - last_flush >= gw["flush_every_s"]
                while self.outbox and (len(self.outbox) >= gw["batch_max"] or flush_due):
                    batch, self.outbox = self.outbox[: gw["batch_max"]], self.outbox[gw["batch_max"]:]
                    asyncio.create_task(send(batch))
                    last_flush = time.monotonic()
                await asyncio.sleep(TICK_S)
