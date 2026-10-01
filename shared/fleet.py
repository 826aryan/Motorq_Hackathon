"""Deterministic fleet definition: same seed + config + graph -> same vehicles every time.

The simulator uses it to drive vehicles; the DB seeder (step 4) uses it to create matching rows.
"""
from dataclasses import dataclass

import numpy as np

from shared.roadgraph import RoadGraph

PERSONAS = ("normal", "towed", "absconding", "tampered", "convoy")


def vehicle_id(i: int) -> str:
    return f"VH-{i + 1:06d}"


@dataclass
class Fleet:
    """Column arrays, one row per vehicle (compact enough for 100k)."""
    oem: np.ndarray            # 'A' | 'B' | 'C'
    persona: np.ndarray        # one of PERSONAS
    home: np.ndarray           # graph node index
    work: np.ndarray
    emit_interval_s: np.ndarray
    depart_s: np.ndarray       # seconds after local midnight
    return_s: np.ndarray
    convoy_group: np.ndarray   # -1 if not in a convoy
    incident_real_s: np.ndarray  # real seconds after launch when the persona acts (nan for normal)
    worker: np.ndarray

    def __len__(self) -> int:
        return len(self.oem)


def build_fleet(cfg: dict, graph: RoadGraph) -> Fleet:
    rng = np.random.default_rng(cfg["seed"])
    f, p = cfg["fleet"], cfg["personas"]
    n = f["vehicles"]

    oems = list(f["oem_share"])
    oem = rng.choice(oems, size=n, p=[f["oem_share"][k] for k in oems])
    core = core_nodes(graph, cfg["graph"]["center"], f["home_radius_m"])
    home = rng.choice(core, size=n)
    work = _pick_at_distance(rng, graph, home, *f["work_distance_m"], pool=core)
    emit = rng.uniform(*f["emit_interval_s"], size=n)
    depart = rng.uniform(f["depart_hour"][0] * 3600, f["depart_hour"][1] * 3600, size=n)
    ret = rng.uniform(f["return_hour"][0] * 3600, f["return_hour"][1] * 3600, size=n)

    persona = np.full(n, "normal", dtype=object)
    convoy_group = np.full(n, -1)
    order = rng.permutation(n)
    cursor = 0
    for name in ("towed", "absconding", "tampered"):
        k = round(n * p[name])
        persona[order[cursor:cursor + k]] = name
        cursor += k

    # Convoys: contiguous IDs so a group stays together; followers share the leader's home.
    groups = int(n / 1000 * p["convoy_groups_per_1000"])
    start = n - 1
    for gidx in range(groups):
        size = int(rng.integers(3, 7))
        members = np.arange(start - size + 1, start + 1)
        start -= size
        if members[0] < 0:
            break
        persona[members] = "convoy"
        convoy_group[members] = gidx
        for col in (home, work, depart, ret):
            col[members] = col[members[0]]

    incident = np.full(n, np.nan)
    special = persona != "normal"
    incident[special] = rng.uniform(*p["incident_after_real_s"], size=int(special.sum()))
    for gidx in range(groups):   # a convoy starts moving together
        members = convoy_group == gidx
        if members.any():
            incident[members] = incident[members].min()

    workers = f["workers"]
    worker = np.arange(n) % workers
    for gidx in range(groups):   # keep a convoy on one worker so members share one clock and path
        members = np.flatnonzero(convoy_group == gidx)
        if len(members):
            worker[members] = members[0] % workers

    return Fleet(oem, persona, home, work, emit, depart, ret, convoy_group, incident, worker)


def distance_from(graph: RoadGraph, center) -> np.ndarray:
    """Metres from every graph node to a (lat, lon) point."""
    dx = (graph.lon - center[1]) * 111_320.0 * np.cos(np.radians(center[0]))
    dy = (graph.lat - center[0]) * 110_540.0
    return np.hypot(dx, dy)


def core_nodes(graph: RoadGraph, center, radius_m: float) -> np.ndarray:
    return np.flatnonzero(distance_from(graph, center) <= radius_m)


def _pick_at_distance(rng, graph: RoadGraph, origin: np.ndarray, lo_m: float, hi_m: float,
                      candidates: int = 24, pool: np.ndarray | None = None) -> np.ndarray:
    """For each origin node pick a random node lo..hi metres away (closest to the band if none fits)."""
    n = len(origin)
    cand = rng.choice(pool, size=(n, candidates)) if pool is not None else rng.integers(0, graph.n_nodes, size=(n, candidates))
    olat, olon = graph.lat[origin][:, None], graph.lon[origin][:, None]
    dx = (graph.lon[cand] - olon) * 111_320.0 * np.cos(np.radians(olat))
    dy = (graph.lat[cand] - olat) * 110_540.0
    dist = np.hypot(dx, dy)
    miss = np.where((dist >= lo_m) & (dist <= hi_m), 0.0, np.minimum(abs(dist - lo_m), abs(dist - hi_m)))
    return cand[np.arange(n), np.argmin(miss + rng.random((n, candidates)) * 1e-3, axis=1)]
