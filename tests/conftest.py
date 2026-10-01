import numpy as np
import pytest

from shared.roadgraph import RoadGraph


@pytest.fixture
def grid_graph() -> RoadGraph:
    """A 10x10 two-way street grid (~110 m blocks) with varied speeds, so fastest != shortest."""
    n = 10
    lat, lon, eu, ev, ln, sp = [], [], [], [], [], []
    for r in range(n):
        for c in range(n):
            lat.append(12.95 + r * 0.001)
            lon.append(77.58 + c * 0.001)
    rng = np.random.default_rng(0)
    for r in range(n):
        for c in range(n):
            i = r * n + c
            for j in ([i + 1] if c < n - 1 else []) + ([i + n] if r < n - 1 else []):
                speed = float(rng.choice([20, 40, 60]))
                for a, b in ((i, j), (j, i)):
                    eu.append(a); ev.append(b); ln.append(110.0); sp.append(speed)
    return RoadGraph.from_edges(lat, lon, eu, ev, ln, sp)


class FakeBloom:
    """Exact in-memory stand-in for RedisBloom (no false positives), for unit and chaos tests."""

    def __init__(self):
        self.sets: dict[str, set] = {}
        self.reserved: list[str] = []

    def reserve(self, key, error_rate, capacity, ttl_s):
        self.reserved.append(key)
        self.sets.setdefault(key, set())

    def mexists(self, key, items):
        s = self.sets.get(key, set())
        return [i in s for i in items]

    def madd(self, key, items):
        s = self.sets.setdefault(key, set())
        out = []
        for i in items:
            out.append(i not in s)
            s.add(i)
        return out


@pytest.fixture
def fake_bloom() -> FakeBloom:
    return FakeBloom()
