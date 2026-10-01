"""Convoy detector - connected components (SPEC §4.4). Runs every 5 minutes over all moving vehicles.

1. Bucket moving vehicles by geohash-6 cell; compare a vehicle only with others in its cell and the
   8 neighbours (instead of all 100k x 100k pairs).
2. Close together, similar heading and similar speed -> a "seen together" edge; its counter goes up.
3. Keep edges seen together N runs in a row (6 runs = 30 min). Latest positions come from events 10-30 s
   apart, so one noisy run is forgiven: an edge may miss up to `max_missed_runs` runs in a row without
   losing its streak (it just doesn't count them); missing more drops it.
4. Union-find over the kept edges gives connected components; size >= 3 -> CONVOY.
"""
from collections import defaultdict
from dataclasses import dataclass

from shared.geo import geohash_neighbours, haversine_m


@dataclass
class LiveState:
    vehicle_id: str
    lat: float
    lon: float
    speed_kmh: float
    heading_deg: float
    geohash: str


class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:              # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self) -> list[set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for x in self.parent:
            out[self.find(x)].add(x)
        return list(out.values())


def heading_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


class ConvoyDetector:
    def __init__(self, cfg: dict):
        self.c = cfg
        self.edges: dict[tuple[str, str], int] = {}     # (a, b) with a < b -> runs seen together in the streak
        self.missed: dict[tuple[str, str], int] = {}    # consecutive runs the edge was not seen
        self.pairs_compared = 0

    def together(self, a: LiveState, b: LiveState) -> bool:
        return (haversine_m(a.lat, a.lon, b.lat, b.lon) <= self.c["max_distance_m"]
                and heading_diff(a.heading_deg, b.heading_deg) <= self.c["max_heading_diff_deg"]
                and abs(a.speed_kmh - b.speed_kmh) <= self.c["max_speed_diff_kmh"])

    def run(self, vehicles: list[LiveState]) -> list[set[str]]:
        """One 5-minute run. Returns convoys (components of >= min_size) seen long enough."""
        moving = [v for v in vehicles if v.speed_kmh >= self.c["moving_kmh"]]
        buckets: dict[str, list[LiveState]] = defaultdict(list)
        for v in moving:
            buckets[v.geohash[:6]].append(v)

        seen_now: set[tuple[str, str]] = set()
        for cell, members in buckets.items():
            nearby = [v for c in (cell, *geohash_neighbours(cell)) for v in buckets.get(c, ())]
            for a in members:
                for b in nearby:
                    if a.vehicle_id < b.vehicle_id:           # each pair once
                        self.pairs_compared += 1
                        if self.together(a, b):
                            seen_now.add((a.vehicle_id, b.vehicle_id))

        # Count seen edges; forgive short gaps; forget edges missing for longer.
        grace = self.c.get("max_missed_runs", 0)
        edges, missed = {}, {}
        for e in seen_now | set(self.edges):
            if e in seen_now:
                edges[e] = self.edges.get(e, 0) + 1
            elif self.missed.get(e, 0) < grace:
                edges[e], missed[e] = self.edges[e], self.missed.get(e, 0) + 1
        self.edges, self.missed = edges, missed

        uf = UnionFind()
        for (a, b), runs in self.edges.items():
            if runs >= self.c["runs_required"]:
                uf.union(a, b)
        return [g for g in uf.groups() if len(g) >= self.c["min_size"]]
