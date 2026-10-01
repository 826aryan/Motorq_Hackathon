import heapq
import random

import pytest

from shared import privacy
from shared.topk import top_k


# ---- Top-K min-heap ----
def test_top_k_matches_full_sort():
    rng = random.Random(1)
    items = [(f"v{i}", rng.random() * 100) for i in range(5000)]
    assert top_k(items, 20) == sorted(items, key=lambda x: -x[1])[:20]


def test_top_k_fewer_items_than_k():
    assert top_k([("a", 1.0), ("b", 3.0)], 10) == [("b", 3.0), ("a", 1.0)]


# ---- privacy ----
def test_pseudonyms_differ_per_partner_and_are_stable():
    k1, k2 = privacy.partner_key("master", "insurer-a"), privacy.partner_key("master", "insurer-b")
    assert privacy.pseudonym(k1, "VH-000001") == privacy.pseudonym(k1, "VH-000001")
    assert privacy.pseudonym(k1, "VH-000001") != privacy.pseudonym(k2, "VH-000001")
    assert "VH-000001" not in privacy.pseudonym(k1, "VH-000001")


def test_k_anonymity_hides_small_cells_and_coarsens():
    fleet = [{"geohash": "tdr1wxyz", "speed_kmh": 30} for _ in range(12)] + \
            [{"geohash": "tdr4abcd", "speed_kmh": 0} for _ in range(3)]
    cells = privacy.cell_insights(fleet, k=10, precision=5)
    assert [c["cell"] for c in cells] == ["tdr1w"]
    assert cells[0]["vehicles"] == 12 and cells[0]["moving_share"] == 1.0


def test_events_are_pseudonymous_hour_rounded_and_dropped_in_rare_cells():
    key = privacy.partner_key("m", "p")
    events = [{"vehicle_id": "VH-000001", "code": "GEOFENCE_EXIT", "ts": 1_790_003_725.0, "geohash": "tdr1wxy"},
              {"vehicle_id": "VH-000002", "code": "TOW_SUSPECTED", "ts": 1_790_003_725.0, "geohash": "tdr4aaa"}]
    out = privacy.anonymize_events(events, key, {"tdr1w": 50, "tdr4a": 2})
    assert len(out) == 1
    assert out[0]["hour"] % 3600 == 0 and out[0]["cell"] == "tdr1w"
    assert set(out[0]) == {"vehicle", "event", "hour", "cell"}          # nothing else leaks


# ---- routing: nearest agent via one Dijkstra on the reversed graph ----
def test_reversed_dijkstra_equals_best_of_per_agent_astar(grid_graph):
    rev = grid_graph.reversed()
    vehicle, agents = 55, {3, 90, 99, 27}
    node, path, cost = rev.dijkstra(vehicle, agents)
    per_agent = {a: grid_graph.astar(a, vehicle)[1] for a in agents}
    assert node == min(per_agent, key=per_agent.get)
    assert cost == pytest.approx(per_agent[node])
    assert path[0] == vehicle and path[-1] == node       # walked vehicle -> agent on the reversed graph


def test_reversed_graph_flips_edges(grid_graph):
    rev = grid_graph.reversed()
    total = sum(len(e) for e in grid_graph.adj)
    assert sum(len(e) for e in rev.adj) == total
    u, (v, _, t) = 0, grid_graph.adj[0][0]
    assert any(w == u and tt == t for w, _, tt in rev.adj[v])


# ---- auth ----
def test_jwt_round_trip_and_tamper(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("DEMO_PASSWORD", "demo-pass")
    from api.auth import check_password, decode_token, issue_token
    tok = issue_token(2, "Lender Beta")
    assert decode_token(tok)["lender_id"] == 2
    with pytest.raises(Exception, match="invalid"):
        decode_token(tok[:-2] + ("A" if tok[-1] != "A" else "B"))
    assert check_password("demo-pass") and not check_password("demo-pas")


def test_jwt_refuses_short_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "short")
    from api.auth import issue_token
    with pytest.raises(RuntimeError):
        issue_token(1, "x")


# ---- storage row format ----
def test_storage_row_is_tab_separated_with_late_flag():
    from storage.rows import COLUMNS, to_row
    e = {"vehicle_id": "VH-000001", "device_ts": 1_790_000_000.25, "event_id": "abc", "oem": "B", "seq": 3,
         "ingest_ts": 1_790_000_001.0, "lat": 12.9, "lon": 77.5, "geohash": "tdr1wxy", "speed_kmh": 10.0,
         "heading_deg": 90.0, "ignition": False, "battery_v": 4.1, "odometer_km": 5.0, "flags": ["suspect_gps"]}
    cols = to_row(e, late=True).rstrip("\n").split("\t")
    assert len(cols) == len(COLUMNS)
    assert cols[1].endswith(".250000+00") and cols[11] == "f" and cols[-1] == "{suspect_gps,late}"


def test_heapq_is_what_top_k_uses():
    assert heapq.nlargest(3, [5, 1, 9, 7]) == [s for _, s in top_k([(str(i), i) for i in [5, 1, 9, 7]], 3)]
