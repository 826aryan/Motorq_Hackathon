"""Routing service - Dijkstra / A* (SPEC §7).

The road graph is loaded into memory once. Edge weight = length / typical speed (travel time).
  Agent -> one vehicle:   A* with the straight-line heuristic -> path + ETA.
  Which agent is closest: ONE Dijkstra from the vehicle over the reversed graph (travel times *to* the
                          vehicle), stopping at the first agent reached - instead of one A* per agent.
Recovery agents are simulated: each gets a position on the road graph, kept in the Redis GEO set agents:live.
"""
import itertools

import numpy as np

from shared.roadgraph import RoadGraph


class Router:
    def __init__(self, graph_path: str, speed_factor: float):
        self.graph = RoadGraph.load(graph_path, speed_factor)
        self.reverse = self.graph.reversed()

    def route(self, from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> dict | None:
        src, dst = self.graph.nearest_node(from_lat, from_lon), self.graph.nearest_node(to_lat, to_lon)
        found = self.graph.astar(src, dst)
        if found is None:
            return None
        nodes, seconds = found
        return {"eta_s": round(seconds), "distance_m": round(self._length(nodes)), "path": self._coords(nodes)}

    def nearest_agent(self, lat: float, lon: float, agents: dict[str, tuple[float, float]]) -> dict | None:
        """agents: agent_id -> (lat, lon). One Dijkstra run from the vehicle, stopping at the first agent."""
        if not agents:
            return None
        node_to_agent = {self.graph.nearest_node(a_lat, a_lon): aid for aid, (a_lat, a_lon) in agents.items()}
        found = self.reverse.dijkstra(self.graph.nearest_node(lat, lon), set(node_to_agent))
        if found is None:
            return None
        node, path, seconds = found
        path.reverse()                                    # reversed graph walked vehicle -> agent
        return {"agent_id": node_to_agent[node], "eta_s": round(seconds),
                "distance_m": round(self._length(path)), "path": self._coords(path)}

    def random_positions(self, n: int, seed: int, center: tuple[float, float], radius_m: float) -> list[tuple[float, float]]:
        g = self.graph
        dx = (g.lon - center[1]) * 111_320.0 * np.cos(np.radians(center[0]))
        dy = (g.lat - center[0]) * 110_540.0
        pool = np.flatnonzero(np.hypot(dx, dy) <= radius_m)
        nodes = np.random.default_rng(seed).choice(pool, size=n)
        return [(float(g.lat[i]), float(g.lon[i])) for i in nodes]

    def _coords(self, nodes: list[int]) -> list[list[float]]:
        return [[round(float(self.graph.lon[i]), 6), round(float(self.graph.lat[i]), 6)] for i in nodes]  # GeoJSON order

    def _length(self, nodes: list[int]) -> float:
        total = 0.0
        for u, v in itertools.pairwise(nodes):
            total += min((ln for w, ln, _ in self.graph.adj[u] if w == v), default=0.0)
        return total
