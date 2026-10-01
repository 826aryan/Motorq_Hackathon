from pathlib import Path

import yaml

from shared.detect.convoy import ConvoyDetector, LiveState, UnionFind, heading_diff
from shared.detect.route import RouteDeviationDetector
from shared.detect.signals import ROUTE_DEVIATION
from shared.geo import geohash_encode, offset_m
from shared.schema import CanonicalEvent

CFG = yaml.safe_load(Path("detectors/config.yaml").read_text(encoding="utf-8"))


def ev(seq, lat, lon, ign=True, vid="VH-000001"):
    return CanonicalEvent(f"{vid}-{seq}", vid, "A", seq, 1_790_000_000.0 + seq * 20, 0.0,
                          lat, lon, geohash_encode(lat, lon, 7), 30.0, 0.0, ign, 4.1, 1.0)


# ---- route deviation (grid_graph: 10x10 nodes from 12.95,77.58, ~110 m apart) ----
def _route(grid_graph, **over):
    cfg = {**CFG["route"], "max_off_path_m": 300, "min_off_path_s": 100, **over}
    home, work = geohash_encode(12.95, 77.58, 7), geohash_encode(12.959, 77.589, 7)
    return RouteDeviationDetector(grid_graph, {"VH-000001": [home, work]}, cfg)


def test_on_expected_path_no_signal(grid_graph):
    det = _route(grid_graph)
    det.on_event(ev(1, 12.95, 77.58, ign=False))
    sigs = []
    for s in range(2, 12):                                   # drive along the diagonal toward work
        sigs += det.on_event(ev(s, 12.95 + s * 0.0008, 77.58 + s * 0.0008))
    assert sigs == [] and det.astar_runs == 1


def test_far_off_path_for_long_enough_fires_once(grid_graph):
    det = _route(grid_graph)
    det.on_event(ev(1, 12.95, 77.58, ign=False))
    det.on_event(ev(2, 12.95, 77.58))                        # trip starts: A* home -> work
    away = offset_m(12.95, 77.58, -2000, -2000)
    sigs = [s for k in range(3, 15) for s in det.on_event(ev(k, *away))]
    assert [s.code for s in sigs] == [ROUTE_DEVIATION]


def test_no_baseline_no_route_check(grid_graph):
    det = RouteDeviationDetector(grid_graph, {}, CFG["route"])
    det.on_event(ev(1, 12.95, 77.58, ign=False))
    assert det.on_event(ev(2, 12.95, 77.58)) == [] and det.trips == {}


def test_path_cache_reused_across_trips(grid_graph):
    det = _route(grid_graph)
    for trip in range(3):
        det.on_event(ev(trip * 2 + 1, 12.95, 77.58, ign=False))
        det.on_event(ev(trip * 2 + 2, 12.95, 77.58))
    assert det.astar_runs == 1


# ---- union-find + convoy ----
def test_union_find_groups():
    uf = UnionFind()
    for a, b in [("a", "b"), ("b", "c"), ("x", "y")]:
        uf.union(a, b)
    uf.find("lonely")
    assert sorted(map(sorted, uf.groups())) == [["a", "b", "c"], ["lonely"], ["x", "y"]]


def test_heading_diff_wraps():
    assert heading_diff(350, 10) == 20


def _fleet(t, group_moving=True):
    """3 vehicles driving together north-east, 2 unrelated, and a parked one right next to the group."""
    lat, lon = offset_m(12.97, 77.59, t * 400, t * 400)
    out = [LiveState(f"VH-00000{i}", *offset_m(lat, lon, i * 40, 0), 40 if group_moving else 0, 45, "")
           for i in range(1, 4)]
    out += [LiveState("VH-000009", *offset_m(12.97, 77.59, -3000, 0), 40, 45, ""),
            LiveState("VH-000008", lat, lon, 40, 225, ""),                       # opposite direction
            LiveState("VH-000007", lat, lon, 0, 45, "")]                          # parked
    return [LiveState(v.vehicle_id, v.lat, v.lon, v.speed_kmh, v.heading_deg, geohash_encode(v.lat, v.lon, 7))
            for v in out]


def test_convoy_needs_six_runs_in_a_row():
    det = ConvoyDetector(CFG["convoy"])
    results = [det.run(_fleet(t)) for t in range(6)]
    assert results[:5] == [[]] * 5
    assert results[5] == [{"VH-000001", "VH-000002", "VH-000003"}]


def test_one_noisy_run_is_forgiven():
    det = ConvoyDetector(CFG["convoy"])
    for t in range(3):
        det.run(_fleet(t))
    det.run(_fleet(3, group_moving=False))                  # one bad run
    results = [det.run(_fleet(t)) for t in range(4, 7)]
    assert results[:2] == [[], []] and results[2]           # 6 seen runs reached despite the gap


def test_convoy_counter_resets_when_they_split():
    det = ConvoyDetector({**CFG["convoy"], "max_missed_runs": 0})
    for t in range(4):
        det.run(_fleet(t))
    det.run(_fleet(4, group_moving=False))                  # a run where they are not together (stopped)
    assert all(det.run(_fleet(t)) == [] for t in range(5, 10))
    assert det.run(_fleet(10)) != []


def test_only_nearby_cells_are_compared():
    det = ConvoyDetector(CFG["convoy"])
    det.run(_fleet(0))
    assert det.pairs_compared < 15                         # 6 vehicles -> 15 pairs if compared blindly
