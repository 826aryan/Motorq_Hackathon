"""Stage 4 - Noise filter (SPEC §3), on events already in event-time order.

- Implied speed = distance / time since the last trusted fix. Above 250 km/h -> flag suspect_gps.
  Not dropped: a GPS jump is itself a tamper signal for the detectors.
- Stopped and moved less than 15 m -> snap to the previous point (parked-car GPS wobble).
"""
from dataclasses import dataclass

from shared.geo import geohash_encode, haversine_m
from shared.schema import CanonicalEvent

SUSPECT_GPS = "suspect_gps"


@dataclass
class _LastFix:
    lat: float
    lon: float
    ts: float


class NoiseFilter:
    def __init__(self, stopped_kmh: float = 1.0, jitter_snap_m: float = 15, max_implied_kmh: float = 250):
        self.stopped_kmh = stopped_kmh
        self.jitter_snap_m = jitter_snap_m
        self.max_implied_kmh = max_implied_kmh
        self.last: dict[str, _LastFix] = {}
        self.snapped = 0
        self.flagged = 0

    def apply(self, e: CanonicalEvent) -> CanonicalEvent:
        prev = self.last.get(e.vehicle_id)
        if prev is None:
            self.last[e.vehicle_id] = _LastFix(e.lat, e.lon, e.device_ts)
            return e
        dist = haversine_m(prev.lat, prev.lon, e.lat, e.lon)
        dt = e.device_ts - prev.ts
        if dt > 0 and dist / dt * 3.6 > self.max_implied_kmh:
            # Keep the last trusted fix as the reference, so the jump back is judged against it too.
            e.flags.append(SUSPECT_GPS)
            self.flagged += 1
            return e
        if e.speed_kmh < self.stopped_kmh and dist < self.jitter_snap_m:
            e.lat, e.lon = prev.lat, prev.lon
            e.geohash = geohash_encode(e.lat, e.lon, 7)
            self.snapped += 1
        self.last[e.vehicle_id] = _LastFix(e.lat, e.lon, e.device_ts)
        return e
