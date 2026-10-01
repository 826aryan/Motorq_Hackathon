"""Route deviation detector - A* (SPEC §4.3).

The nightly baseline gives each vehicle its usual destinations (home cell, work cell). When a trip
starts (ignition goes on), the vehicle is assumed to head for the usual destination it is not at, and
A* on the road graph computes the expected path once (cached). Heuristic = straight-line distance /
max road speed, so A* stays optimal. Every event of the trip is measured against that path:
ROUTE_DEVIATION once it has stayed more than 2 km off the path for 5 minutes.
"""
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

from shared.detect.signals import ROUTE_DEVIATION, Signal
from shared.geo import geohash_bbox, haversine_m
from shared.roadgraph import RoadGraph
from shared.schema import CanonicalEvent


@dataclass
class _Trip:
    lat: np.ndarray            # expected path, as node coordinates
    lon: np.ndarray
    off_since: float | None = None
    reported: bool = False


def cell_centre(gh: str) -> tuple[float, float]:
    lat_lo, lat_hi, lon_lo, lon_hi = geohash_bbox(gh)
    return (lat_lo + lat_hi) / 2, (lon_lo + lon_hi) / 2


class RouteDeviationDetector:
    def __init__(self, graph: RoadGraph, destinations: dict[str, list[str]], cfg: dict):
        self.graph = graph
        self.destinations = destinations          # vehicle -> usual destination geohash cells
        self.max_off_m = cfg["max_off_path_m"]
        self.min_off_s = cfg["min_off_path_s"]
        self.moving_kmh = cfg["moving_kmh"]
        self.cache: OrderedDict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = OrderedDict()
        self.cache_size = cfg["path_cache_size"]
        self.trips: dict[str, _Trip] = {}
        self.ignition: dict[str, bool] = {}
        self.astar_runs = 0

    def on_event(self, e: CanonicalEvent) -> list[Signal]:
        if "suspect_gps" in e.flags:
            return []
        was_on = self.ignition.get(e.vehicle_id, False)
        self.ignition[e.vehicle_id] = e.ignition
        if not e.ignition:
            self.trips.pop(e.vehicle_id, None)    # trip over
            return []
        if not was_on:
            self._start_trip(e)
        trip = self.trips.get(e.vehicle_id)
        if trip is None:
            return []
        if self.distance_to_path_m(trip, e.lat, e.lon) <= self.max_off_m:
            trip.off_since = None
            return []
        if trip.off_since is None:
            trip.off_since = e.device_ts
        if not trip.reported and e.device_ts - trip.off_since >= self.min_off_s:
            trip.reported = True
            return [Signal(e.vehicle_id, ROUTE_DEVIATION, e.device_ts, e.lat, e.lon,
                           detail={"off_path_s": round(e.device_ts - trip.off_since)})]
        return []

    def _start_trip(self, e: CanonicalEvent) -> None:
        cells = self.destinations.get(e.vehicle_id)
        if not cells:
            return                                 # no baseline yet: nothing to compare against
        # Head for the usual destination that is furthest from here (i.e. not the one we are at).
        dest = max(cells, key=lambda c: haversine_m(e.lat, e.lon, *cell_centre(c)))
        src_node = self.graph.nearest_node(e.lat, e.lon)
        dst_node = self.graph.nearest_node(*cell_centre(dest))
        path = self._path(src_node, dst_node)
        if path is not None:
            self.trips[e.vehicle_id] = _Trip(*path)

    def _path(self, src: int, dst: int) -> tuple[np.ndarray, np.ndarray] | None:
        key = (src, dst)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        self.astar_runs += 1
        found = self.graph.astar(src, dst) if src != dst else ([src], 0.0)
        if found is None:
            return None
        nodes = found[0]
        path = (self.graph.lat[nodes], self.graph.lon[nodes])
        self.cache[key] = path
        if len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)
        return path

    @staticmethod
    def distance_to_path_m(trip: _Trip, lat: float, lon: float) -> float:
        """Distance to the nearest path node (nodes are ~100 m apart: plenty for a 2 km threshold)."""
        dx = (trip.lon - lon) * 111_320.0 * np.cos(np.radians(lat))
        dy = (trip.lat - lat) * 110_540.0
        return float(np.sqrt(np.min(dx * dx + dy * dy)))
