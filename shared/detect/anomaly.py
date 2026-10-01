"""Anomaly detector - sliding window (SPEC §4.2).

Each vehicle keeps a 10-minute window (deque) of its recent points; old points fall off the front as
new ones arrive. Rules, all evaluated inside the window:
  TOW_SUSPECTED     ignition off throughout, yet moved more than 500 m
  NIGHT_MOVEMENT    driving 00:00-05:00 local when the baseline says it is usually parked
  TAMPER_SUSPECTED  suspect_gps or a battery voltage drop, followed by silence over 15 min
  SPEED_SPIKE       average moving speed above the vehicle's baseline mean + 3 std dev
Timestamps are event time (device_ts), so replaying history in the batch job gives the same signals.
"""
import itertools
import statistics
from collections import deque
from dataclasses import dataclass

from shared.detect.signals import NIGHT_MOVEMENT, SPEED_SPIKE, TAMPER_SUSPECTED, TOW_SUSPECTED, Signal
from shared.geo import haversine_m
from shared.schema import CanonicalEvent


@dataclass
class Point:
    ts: float
    lat: float
    lon: float
    speed_kmh: float
    ignition: bool
    battery_v: float
    suspect: bool


@dataclass
class Baseline:
    active_hours: tuple[int, int] | None    # usual driving hours [start, end) local
    avg_speed: float | None
    std_speed: float | None


class AnomalyDetector:
    def __init__(self, cfg: dict, baselines: dict[str, Baseline] | None = None, tz_offset_h: float = 5.5):
        self.c = cfg
        self.baselines = baselines or {}
        self.tz_s = tz_offset_h * 3600
        self.windows: dict[str, deque[Point]] = {}
        self.tamper_precursor: dict[str, Point] = {}    # suspicious point, waiting to see if silence follows
        self.last_seen: dict[str, float] = {}
        self.last_seq: dict[str, int] = {}       # the pipeline releases each session in seq order
        self.clock = 0.0                                 # newest event time seen: our "now" in event time

    def on_event(self, e: CanonicalEvent) -> list[Signal]:
        p = Point(e.device_ts, e.lat, e.lon, e.speed_kmh, e.ignition, e.battery_v, "suspect_gps" in e.flags)
        w = self.windows.setdefault(e.vehicle_id, deque())
        if e.seq < self.last_seq.get(e.vehicle_id, -1):
            # New device session (tracker reboot): positions before it can't be compared with this one.
            w.clear()
            self.tamper_precursor.pop(e.vehicle_id, None)
        self.last_seq[e.vehicle_id] = e.seq
        w.append(p)
        while w[0].ts < p.ts - self.c["window_s"]:
            w.popleft()
        self.last_seen[e.vehicle_id] = p.ts
        self.clock = max(self.clock, p.ts)

        signals = []
        for rule in (self._tow, self._night, self._speed_spike):
            sig = rule(e.vehicle_id, w, p)
            if sig:
                signals.append(sig)
        self._note_tamper_precursor(e.vehicle_id, w, p)
        return signals

    def tick(self) -> list[Signal]:
        """Called periodically: fire TAMPER for vehicles that went silent after a suspicious point."""
        signals = []
        for vid, p in list(self.tamper_precursor.items()):
            silent_for = self.clock - self.last_seen[vid]
            if silent_for >= self.c["tamper_silence_s"]:
                del self.tamper_precursor[vid]
                signals.append(Signal(vid, TAMPER_SUSPECTED, p.ts, p.lat, p.lon,
                                      detail={"silent_s": round(silent_for)}))
        return signals

    # ---- rules ----
    def _tow(self, vid: str, w: deque[Point], p: Point) -> Signal | None:
        if p.ignition or p.suspect:
            return None
        run = []                          # the ignition-off run still in the window, newest first
        for q in reversed(w):
            if q.ignition:
                break
            if not q.suspect:
                run.append(q)
        need = self.c["tow_confirm_points"]
        if len(run) < 2 * need:
            return None
        # Where it was parked: the median of the oldest `need` points, so one glitch can't be the start.
        oldest = run[-need:]
        start = Point(0, statistics.median(q.lat for q in oldest), statistics.median(q.lon for q in oldest),
                      0, False, 0, False)
        moved = haversine_m(start.lat, start.lon, p.lat, p.lon)
        if moved <= self.c["tow_min_m"]:
            return None
        # Sustained, not a glitch: the last `need` points must all be displaced from the start and each
        # step between them plausible. A 1-5 km GPS glitch can stay under the noise filter's 250 km/h
        # limit, and a tampered tracker's random jumps rarely line up several times in a row.
        recent = run[:need]
        if any(haversine_m(start.lat, start.lon, q.lat, q.lon) <= self.c["tow_min_m"] / 2 for q in recent):
            return None
        if any(haversine_m(a.lat, a.lon, b.lat, b.lon) > self.c["tow_min_m"] for a, b in itertools.pairwise(recent)):
            return None
        strength = min(1.0, moved / self.c["tow_full_strength_m"])
        return Signal(vid, TOW_SUSPECTED, p.ts, p.lat, p.lon, strength, {"moved_m": round(moved)})

    def _night(self, vid: str, w: deque[Point], p: Point) -> Signal | None:
        hour = ((p.ts + self.tz_s) % 86_400) / 3600
        start, end = self.c["night_hours"]
        if not (start <= hour < end and p.ignition and p.speed_kmh >= self.c["moving_kmh"]):
            return None
        b = self.baselines.get(vid)
        if b and b.active_hours and b.active_hours[0] <= hour < b.active_hours[1]:
            return None                   # this vehicle normally drives at this hour
        return Signal(vid, NIGHT_MOVEMENT, p.ts, p.lat, p.lon, detail={"local_hour": int(hour)})

    def _speed_spike(self, vid: str, w: deque[Point], p: Point) -> Signal | None:
        b = self.baselines.get(vid)
        if not b or b.avg_speed is None or b.std_speed is None:
            return None                   # needs the nightly baseline (step 6)
        moving = [q.speed_kmh for q in w if q.speed_kmh >= self.c["moving_kmh"] and not q.suspect]
        if len(moving) < self.c["speed_spike_min_points"]:
            return None
        avg = sum(moving) / len(moving)
        limit = b.avg_speed + self.c["speed_spike_std"] * b.std_speed
        if avg <= limit:
            return None
        return Signal(vid, SPEED_SPIKE, p.ts, p.lat, p.lon, detail={"avg_kmh": round(avg), "limit_kmh": round(limit)})

    def _note_tamper_precursor(self, vid: str, w: deque[Point], p: Point) -> None:
        prev = self.tamper_precursor.get(vid)
        if prev and p.ts - prev.ts > self.c["tamper_silence_s"]:
            del self.tamper_precursor[vid]      # it kept reporting normally: not tampering
        voltage_drop = max(q.battery_v for q in w) - p.battery_v >= self.c["battery_drop_v"]
        if p.suspect or voltage_drop or p.battery_v < self.c["battery_low_v"]:
            self.tamper_precursor.setdefault(vid, p)
