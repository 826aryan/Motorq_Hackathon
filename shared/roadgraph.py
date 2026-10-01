"""In-memory road graph with our own A* and Dijkstra (SPEC §2, §4.3, §7 routing).

Nodes are intersections, edge weight = travel time in seconds (length / typical speed).
The graph file is produced once by simulator/build_graph.py from OpenStreetMap.
"""
import heapq
from dataclasses import dataclass

import numpy as np

from shared.geo import haversine_m


@dataclass
class RoadGraph:
    lat: np.ndarray                                   # node latitude, index = node id
    lon: np.ndarray
    adj: list[list[tuple[int, float, float]]]        # node -> [(neighbour, length_m, travel_s)]
    max_speed_mps: float                             # fastest edge; keeps the A* heuristic admissible

    @classmethod
    def from_edges(cls, lat, lon, edge_u, edge_v, edge_len_m, edge_speed_kmh, speed_factor: float = 1.0):
        adj: list[list[tuple[int, float, float]]] = [[] for _ in range(len(lat))]
        max_mps = 0.1
        for u, v, length, kmh in zip(edge_u, edge_v, edge_len_m, edge_speed_kmh, strict=True):
            mps = float(kmh) * speed_factor / 3.6
            max_mps = max(max_mps, mps)
            adj[int(u)].append((int(v), float(length), float(length) / mps))
        return cls(np.asarray(lat, dtype=float), np.asarray(lon, dtype=float), adj, max_mps)

    @classmethod
    def load(cls, path: str, speed_factor: float = 1.0) -> "RoadGraph":
        d = np.load(path)
        return cls.from_edges(d["node_lat"], d["node_lon"], d["edge_u"], d["edge_v"],
                              d["edge_len_m"], d["edge_speed_kmh"], speed_factor)

    def reversed(self) -> "RoadGraph":
        """Same roads with every edge flipped: Dijkstra from X on it gives travel times *to* X."""
        adj: list[list[tuple[int, float, float]]] = [[] for _ in range(self.n_nodes)]
        for u, edges in enumerate(self.adj):
            for v, length, t in edges:
                adj[v].append((u, length, t))
        return RoadGraph(self.lat, self.lon, adj, self.max_speed_mps)

    @property
    def n_nodes(self) -> int:
        return len(self.lat)

    def nearest_node(self, lat: float, lon: float) -> int:
        # Equirectangular distance is fine for picking the closest node inside one city.
        dx = (self.lon - lon) * np.cos(np.radians(lat))
        dy = self.lat - lat
        return int(np.argmin(dx * dx + dy * dy))

    def _heuristic_s(self, a: int, b: int) -> float:
        return haversine_m(self.lat[a], self.lon[a], self.lat[b], self.lon[b]) / self.max_speed_mps

    def astar(self, src: int, dst: int) -> tuple[list[int], float] | None:
        """Fastest path by travel time. Heuristic = straight-line distance / max road speed."""
        g = {src: 0.0}
        came: dict[int, int] = {}
        open_heap = [(self._heuristic_s(src, dst), src)]
        closed = set()
        while open_heap:
            _, u = heapq.heappop(open_heap)
            if u == dst:
                return _rebuild(came, dst), g[dst]
            if u in closed:
                continue
            closed.add(u)
            for v, _length, t in self.adj[u]:
                cand = g[u] + t
                if cand < g.get(v, float("inf")):
                    g[v] = cand
                    came[v] = u
                    heapq.heappush(open_heap, (cand + self._heuristic_s(v, dst), v))
        return None

    def dijkstra(self, src: int, targets: set[int]) -> tuple[int, list[int], float] | None:
        """Grow outwards from src and stop at the first target reached (e.g. the nearest agent)."""
        dist = {src: 0.0}
        came: dict[int, int] = {}
        heap = [(0.0, src)]
        done = set()
        while heap:
            d, u = heapq.heappop(heap)
            if u in done:
                continue
            if u in targets:
                return u, _rebuild(came, u), d
            done.add(u)
            for v, _length, t in self.adj[u]:
                cand = d + t
                if cand < dist.get(v, float("inf")):
                    dist[v] = cand
                    came[v] = u
                    heapq.heappush(heap, (cand, v))
        return None


def _rebuild(came: dict[int, int], node: int) -> list[int]:
    path = [node]
    while node in came:
        node = came[node]
        path.append(node)
    path.reverse()
    return path
