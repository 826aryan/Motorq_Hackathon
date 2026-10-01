import math

import numpy as np
import pytest

from shared.fleet import build_fleet
from simulator.vehicle import Planner, Spec, Trip, Vehicle

CFG = {
    "seed": 7,
    "graph": {"center": [12.9545, 77.5845]},
    "fleet": {"vehicles": 2000, "workers": 4, "emit_interval_s": [10, 30],
              "oem_share": {"A": 0.4, "B": 0.35, "C": 0.25}, "home_radius_m": 5000, "work_distance_m": [200, 800],
              "depart_hour": [7, 10], "return_hour": [17, 20], "errands_per_day": 0,
              "errand_distance_m": [100, 300]},
    "personas": {"towed": 0.01, "absconding": 0.01, "tampered": 0.01, "convoy_groups_per_1000": 2,
                 "incident_after_real_s": [60, 600], "towed_speed_kmh": 25, "tamper_jump_s": 120,
                 "tamper_silence_s": 1800, "convoy_legs": 2},
}


def test_trip_starts_and_ends_at_path_ends(grid_graph):
    path, _ = grid_graph.astar(0, 99)
    trip = Trip(grid_graph, path, start_t=1000.0)
    lat0, lon0, *_ = trip.state(1000.0)
    lat1, lon1, speed_end, _, done = trip.state(trip.end_t + 5)
    assert (lat0, lon0) == (grid_graph.lat[0], grid_graph.lon[0])
    assert (lat1, lon1) == (grid_graph.lat[99], grid_graph.lon[99])
    assert speed_end == 0 and done == pytest.approx(trip.length_m)


def test_speed_cap_slows_trip(grid_graph):
    path, _ = grid_graph.astar(0, 99)
    assert Trip(grid_graph, path, 0, speed_cap_kmh=5).end_t > Trip(grid_graph, path, 0).end_t


def test_fleet_is_deterministic_and_has_personas(grid_graph):
    a, b = build_fleet(CFG, grid_graph), build_fleet(CFG, grid_graph)
    assert (a.home == b.home).all() and (a.persona == b.persona).all()
    counts = {p: int((a.persona == p).sum()) for p in set(a.persona)}
    assert counts["towed"] == 20 and counts["convoy"] >= 12
    assert set(a.oem) == {"A", "B", "C"}


def test_convoy_members_share_home_schedule_and_worker(grid_graph):
    f = build_fleet(CFG, grid_graph)
    for g in set(f.convoy_group) - {-1}:
        m = f.convoy_group == g
        for col in (f.home, f.work, f.depart_s, f.worker, f.incident_real_s):
            assert len(set(col[m])) == 1


def _vehicle(grid_graph, persona, incident_t, start_t=0.0):
    spec = Spec(0, "VH-000001", "A", persona, 0, 99, 15.0, 8 * 3600, 18 * 3600, -1, incident_t)
    return Vehicle(spec, Planner(grid_graph, np.random.default_rng(0)), CFG, tz_s=0, start_t=start_t)


def test_normal_vehicle_drives_to_work_after_departure(grid_graph):
    v = _vehicle(grid_graph, "normal", math.inf)
    assert v.reading(7 * 3600).speed_kmh == 0            # parked at home before departure
    moving = v.reading(8 * 3600 + 20)
    assert moving.ignition and moving.speed_kmh > 0
    parked = v.reading(12 * 3600)
    assert parked.speed_kmh == 0 and v.node == 99        # arrived at work


def test_towed_vehicle_moves_with_ignition_off(grid_graph):
    v = _vehicle(grid_graph, "towed", incident_t=3 * 3600)
    v.reading(3 * 3600)
    r = v.reading(3 * 3600 + 30)
    assert not r.ignition and r.speed_kmh > 0


def test_tampered_vehicle_jumps_then_goes_silent(grid_graph):
    v = _vehicle(grid_graph, "tampered", incident_t=3 * 3600)
    first = v.reading(3 * 3600)
    assert first is not None and first.battery_v < 4.1
    assert v.reading(3 * 3600 + 600) is None             # silent after the jump phase
    assert v.reading(3 * 3600 + 120 + 1800 + 10) is not None


def test_paused_vehicle_resumes_where_it_stopped(grid_graph):
    """Load control: switching a vehicle off mid-trip and on again must not make it jump (looks like a tow)."""
    v = _vehicle(grid_graph, "normal", math.inf)
    v.reading(8 * 3600 + 1)                                  # leaves home for work
    mid = 8 * 3600 + 60
    r = v.reading(mid)
    assert r.speed_kmh > 0
    v.pause(mid)
    v.resume(mid + 600)                                      # switched back on 10 minutes later
    back = v.reading(mid + 600)
    assert math.dist((back.lat, back.lon), (r.lat, r.lon)) < 0.002   # same place (one road node, ~200 m)
    assert back.odometer_km >= r.odometer_km
