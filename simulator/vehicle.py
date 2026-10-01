"""Vehicle movement: trips along A* paths, daily schedules, and persona behaviour (SPEC §2).

Position is a pure function of time along a trip (interpolated), so a vehicle does no work
between the moments it emits an event. That is what lets one process drive ~25k vehicles.
"""
import itertools
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

from shared.geo import bearing_deg
from shared.roadgraph import RoadGraph
from simulator.encoders import Reading

DAY_S = 86_400


class Trip:
    """A path through the graph starting at start_t (sim epoch seconds)."""

    def __init__(self, graph: RoadGraph, nodes: list[int], start_t: float,
                 ignition: bool = True, speed_cap_kmh: float | None = None):
        self.nodes = nodes
        self.start_t = start_t
        self.ignition = ignition
        self.lat = graph.lat[nodes]
        self.lon = graph.lon[nodes]
        seg_len, seg_t = [], []
        for u, v in itertools.pairwise(nodes):
            length, t = min(((ln, tt) for w, ln, tt in graph.adj[u] if w == v), default=(0.0, 0.0))
            if speed_cap_kmh:
                t = max(t, length / (speed_cap_kmh / 3.6))
            seg_len.append(length)
            seg_t.append(t)
        self.cum_len = np.concatenate([[0.0], np.cumsum(seg_len)])
        self.cum_t = np.concatenate([[0.0], np.cumsum(seg_t)])
        self.end_t = start_t + float(self.cum_t[-1])

    @property
    def length_m(self) -> float:
        return float(self.cum_len[-1])

    def shifted(self, offset_s: float) -> "Trip":
        """Same path, starting later (convoy followers)."""
        clone = object.__new__(Trip)
        clone.__dict__.update(self.__dict__)
        clone.start_t = self.start_t + offset_s
        clone.end_t = self.end_t + offset_s
        return clone

    def state(self, t: float) -> tuple[float, float, float, float, float]:
        """(lat, lon, speed_kmh, heading_deg, distance_done_m) at sim time t."""
        el = min(max(t - self.start_t, 0.0), float(self.cum_t[-1]))
        i = int(np.searchsorted(self.cum_t, el, side="right")) - 1
        i = min(max(i, 0), len(self.nodes) - 2)
        seg_t = self.cum_t[i + 1] - self.cum_t[i]
        frac = 0.0 if seg_t <= 0 else (el - self.cum_t[i]) / seg_t
        lat = self.lat[i] + (self.lat[i + 1] - self.lat[i]) * frac
        lon = self.lon[i] + (self.lon[i + 1] - self.lon[i]) * frac
        seg_len = self.cum_len[i + 1] - self.cum_len[i]
        speed = 0.0 if seg_t <= 0 or el >= self.cum_t[-1] else seg_len / seg_t * 3.6
        heading = bearing_deg(self.lat[i], self.lon[i], self.lat[i + 1], self.lon[i + 1])
        done = self.cum_len[i] + seg_len * frac
        return float(lat), float(lon), float(speed), heading, float(done)


class Planner:
    """A* with an LRU cache: home<->work trips repeat every day, so most lookups are hits."""

    def __init__(self, graph: RoadGraph, rng: np.random.Generator, cache_size: int = 20_000):
        self.graph = graph
        self.rng = rng
        self.cache: OrderedDict[tuple[int, int], list[int] | None] = OrderedDict()
        self.cache_size = cache_size
        self.hits = self.misses = 0
        self.core_nodes = None      # where normal life happens (set by the worker)
        self.outer_nodes = None     # beyond the lender geofence (absconding targets)

    def path(self, src: int, dst: int) -> list[int] | None:
        key = (src, dst)
        if key in self.cache:
            self.hits += 1
            self.cache.move_to_end(key)
            return self.cache[key]
        self.misses += 1
        found = self.graph.astar(src, dst) if src != dst else None
        nodes = found[0] if found else None
        self.cache[key] = nodes
        if len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)
        return nodes

    def node_near(self, node: int, lo_m: float, hi_m: float, tries: int = 32, pool=None) -> int:
        g = self.graph
        cand = self.rng.choice(pool, size=tries) if pool is not None else self.rng.integers(0, g.n_nodes, size=tries)
        dx = (g.lon[cand] - g.lon[node]) * 111_320.0 * np.cos(np.radians(g.lat[node]))
        dy = (g.lat[cand] - g.lat[node]) * 110_540.0
        d = np.hypot(dx, dy)
        ok = np.flatnonzero((d >= lo_m) & (d <= hi_m))
        return int(cand[ok[0]] if len(ok) else cand[np.argmin(abs(d - (lo_m + hi_m) / 2))])

    def closest_in(self, node: int, pool, tries: int = 64) -> int:
        """The closest of `tries` random nodes from pool (e.g. the quickest way out of the zone)."""
        g = self.graph
        cand = self.rng.choice(pool, size=tries)
        dx = (g.lon[cand] - g.lon[node]) * np.cos(np.radians(g.lat[node]))
        return int(cand[np.argmin(dx * dx + (g.lat[cand] - g.lat[node]) ** 2)])

    def multi_leg(self, start: int, legs: int, lo_m: float, hi_m: float) -> list[int] | None:
        nodes, here = [start], start
        for _ in range(legs):
            nxt = self.node_near(here, lo_m, hi_m)
            leg = self.path(here, nxt)
            if not leg:
                continue
            nodes.extend(leg[1:])
            here = nxt
        return nodes if len(nodes) > 1 else None


@dataclass
class Spec:
    idx: int
    vehicle_id: str
    oem: str
    persona: str
    home: int
    work: int
    emit_interval_s: float
    depart_s: float
    return_s: float
    convoy_group: int
    incident_t: float      # sim time the persona acts; inf for normal


class Vehicle:
    def __init__(self, spec: Spec, planner: Planner, cfg: dict, tz_s: float, start_t: float):
        self.s = spec
        self.planner = planner
        self.fleet_cfg = cfg["fleet"]
        self.p_cfg = cfg["personas"]
        self.tz_s = tz_s
        rng = planner.rng
        self.seq = 0
        self.odometer_km = float(rng.uniform(5_000, 80_000))
        self.battery_v = float(rng.uniform(4.05, 4.15))
        self.trip: Trip | None = None
        self.silent_until = -1.0
        self.jump_until = -1.0
        self.frozen = False          # towed/absconded vehicles stop following their schedule
        self.incident_done = spec.persona == "normal"
        self.waypoints: list[tuple[float, int]] = []
        self._last_t = start_t
        self.node = self._place_at(start_t)

    # ---- schedule ----
    def _local_midnight(self, t: float) -> float:
        return t - ((t + self.tz_s) % DAY_S)

    def _plan_day(self, midnight: float) -> list[tuple[float, int]]:
        s, f = self.s, self.fleet_cfg
        plan = [(midnight + s.depart_s, s.work)]
        if s.persona != "convoy":   # convoy members keep identical schedules so they stay together
            span = s.return_s - s.depart_s
            for _ in range(int(f["errands_per_day"])):
                go = s.depart_s + self.planner.rng.uniform(0.15, 0.75) * span
                stop = self.planner.node_near(s.work, *f["errand_distance_m"], pool=self.planner.core_nodes)
                plan += [(midnight + go, stop), (midnight + go + 1800, s.work)]
        plan.append((midnight + s.return_s, s.home))
        return sorted(plan)

    def _place_at(self, t: float) -> int:
        """Where the vehicle is parked at start-up, and which waypoints are still ahead today."""
        midnight = self._local_midnight(t)
        plan = self._plan_day(midnight) + [(wt + DAY_S, n) for wt, n in self._plan_day(midnight + DAY_S)]
        node = self.s.home
        for wt, n in plan:
            if wt <= t:
                node = n
        self.waypoints = [(wt, n) for wt, n in plan if wt > t]
        return node

    def _next_waypoint(self) -> tuple[float, int]:
        if not self.waypoints:
            midnight = self._local_midnight(self._last_t + DAY_S / 2)
            self.waypoints = [(wt, n) for wt, n in self._plan_day(midnight) if wt > self._last_t]
            if not self.waypoints:
                self.waypoints = self._plan_day(midnight + DAY_S)
        return self.waypoints[0]

    def pause(self, t: float) -> None:
        """Switched off by load control: park at the road node reached so far on the current trip."""
        if self.trip:
            i = int(np.searchsorted(self.trip.cum_t, t - self.trip.start_t, side="right")) - 1
            self.node = self.trip.nodes[max(0, min(i, len(self.trip.nodes) - 1))]
            self.odometer_km += self.trip.state(t)[4] / 1000
            self.trip = None

    def resume(self, t: float) -> None:
        """Switched back on: stay where it was parked (no jump, which would look like a tow) and follow
        the rest of today's schedule from here."""
        here = self.node
        self._place_at(t)               # refreshes the waypoints still ahead today
        self.node = here

    def start_trip(self, trip: Trip) -> None:
        self.trip = trip

    # ---- persona incidents (towed / absconding / tampered; convoy is started by the worker) ----
    def _run_incident(self, t: float) -> None:
        self.incident_done = True
        p, planner, here = self.p_cfg, self.planner, self._current_node()
        if self.s.persona == "towed":
            dest = planner.node_near(here, 2000, 8000)
            nodes = planner.path(here, dest)
            if nodes:
                self.trip = Trip(planner.graph, nodes, t, ignition=False, speed_cap_kmh=p["towed_speed_kmh"])
            self.frozen = True
        elif self.s.persona == "absconding":
            dest = planner.closest_in(here, planner.outer_nodes)
            nodes = planner.path(here, dest)
            if nodes:
                self.trip = Trip(planner.graph, nodes, t)
            self.frozen = True
        elif self.s.persona == "tampered":
            self.jump_until = t + p["tamper_jump_s"]
            self.silent_until = self.jump_until + p["tamper_silence_s"]

    def _current_node(self) -> int:
        return self.trip.nodes[-1] if self.trip else self.node

    # ---- one emission ----
    def reading(self, t: float) -> Reading | None:
        self._last_t = t
        if not self.incident_done and self.s.persona != "convoy" and t >= self.s.incident_t:
            self._run_incident(t)

        if self.trip and t >= self.trip.end_t:        # arrived
            self.odometer_km += self.trip.length_m / 1000
            self.node = self.trip.nodes[-1]
            self.trip = None

        if not self.trip and not self.frozen:
            wt, dest = self._next_waypoint()
            if t >= wt:
                self.waypoints.pop(0)
                nodes = self.planner.path(self.node, dest)
                if nodes:
                    self.trip = Trip(self.planner.graph, nodes, wt)
                else:
                    self.node = dest

        if self.jump_until > t:                       # tampering: voltage sags, GPS jumps
            self.battery_v = max(3.2, self.battery_v - 0.05)
        elif self.silent_until > t:
            return None                               # tracker gone quiet
        elif self.silent_until > 0:
            self.battery_v, self.silent_until = 4.0, -1.0   # tracker back after silence

        self.seq += 1
        if self.trip:
            lat, lon, speed, heading, done = self.trip.state(t)
            ignition = self.trip.ignition
            odo = self.odometer_km + done / 1000
        else:
            g = self.planner.graph
            lat, lon, speed, heading, ignition, odo = g.lat[self.node], g.lon[self.node], 0.0, 0.0, False, self.odometer_km
        if self.jump_until > t:
            rng = self.planner.rng
            lat += float(rng.uniform(-0.03, 0.03))
            lon += float(rng.uniform(-0.03, 0.03))
        if speed > 0:
            speed *= float(self.planner.rng.uniform(0.9, 1.1))
        return Reading(self.s.vehicle_id, self.seq, t, float(lat), float(lon), speed, heading,
                       ignition, self.battery_v, odo)
