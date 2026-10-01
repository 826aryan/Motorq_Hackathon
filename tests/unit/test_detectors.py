from pathlib import Path

import pytest
import yaml

from detectors.engine import DetectorEngine, EngineState
from shared.detect.anomaly import AnomalyDetector, Baseline
from shared.detect.geofence import BORDER, INSIDE, GeofenceDetector, GeofenceIndex, cover
from shared.detect.risk import Loan, RiskScorer
from shared.detect.signals import GEOFENCE_EXIT, NIGHT_MOVEMENT, SPEED_SPIKE, TAMPER_SUSPECTED, TOW_SUSPECTED, Signal
from shared.geo import circle_polygon, geohash_encode, geohash_neighbours, offset_m, point_in_polygon
from shared.schema import CanonicalEvent

CFG = yaml.safe_load(Path("detectors/config.yaml").read_text(encoding="utf-8"))
C = (12.9716, 77.5946)
FENCE = circle_polygon(*C, 6000)
NOON = 1_790_000_000.0 - (1_790_000_000.0 + 5.5 * 3600) % 86_400 + 12 * 3600    # 12:00 local


def ev(seq, lat, lon, *, ts=None, ign=True, speed=30.0, battery=4.1, flags=None, vid="VH-000001"):
    return CanonicalEvent(f"{vid}-{seq}", vid, "A", seq, NOON + seq * 20 if ts is None else ts, 0.0,
                          lat, lon, geohash_encode(lat, lon, 7), speed, 0.0, ign, battery, 1.0, flags or [])


# ---- geohash + polygon helpers ----
def test_point_in_polygon():
    assert point_in_polygon(*C, FENCE)
    assert not point_in_polygon(*offset_m(*C, 7000, 0), FENCE)


def test_neighbours_are_8_distinct_adjacent_cells():
    gh = geohash_encode(*C, 6)
    nb = geohash_neighbours(gh)
    assert len(set(nb)) == 8 and gh not in nb


def test_cover_marks_centre_inside_and_edge_border():
    cells = cover(FENCE, 6)
    assert cells[geohash_encode(*C, 6)] == INSIDE
    assert cells[geohash_encode(*offset_m(*C, 6000, 0), 6)] == BORDER
    assert geohash_encode(*offset_m(*C, 9000, 0), 6) not in cells


def test_index_agrees_with_exact_check_everywhere():
    idx = GeofenceIndex({1: FENCE})
    for north in range(-7000, 7001, 700):
        for east in range(-7000, 7001, 700):
            lat, lon = offset_m(*C, north, east)
            assert idx.contains(1, lat, lon, geohash_encode(lat, lon, 7)) == point_in_polygon(lat, lon, FENCE)


# ---- geofence detector ----
def test_exit_fires_once_after_three_outside_events():
    det = GeofenceDetector(GeofenceIndex({1: FENCE}), {"VH-000001": [1]}, exit_after=3)
    out = offset_m(*C, 7500, 0)
    fired = [det.on_event(ev(s, *out)) for s in range(1, 7)]
    assert [len(f) for f in fired] == [0, 0, 1, 0, 0, 0]
    assert fired[2][0].code == GEOFENCE_EXIT


def test_border_flapping_does_not_fire():
    det = GeofenceDetector(GeofenceIndex({1: FENCE}), {"VH-000001": [1]}, exit_after=3)
    inside, outside = offset_m(*C, 5900, 0), offset_m(*C, 6100, 0)
    seq = [outside, outside, inside, outside, outside, inside]
    assert not any(det.on_event(ev(i + 1, *p)) for i, p in enumerate(seq))


# ---- sliding window ----
def test_tow_fires_when_moved_500m_with_ignition_off():
    det = AnomalyDetector(CFG["anomaly"])
    sigs = []
    for s in range(1, 8):
        sigs += det.on_event(ev(s, *offset_m(*C, s * 120, 0), ign=False, speed=20))
    tows = [x for x in sigs if x.code == TOW_SUSPECTED]
    assert tows and tows[0].detail["moved_m"] > 500


def test_parked_vehicle_and_normal_drive_do_not_tow():
    det = AnomalyDetector(CFG["anomaly"])
    sigs = []
    for s in range(1, 20):
        sigs += det.on_event(ev(s, *C, ign=False, speed=0))                        # parked
        sigs += det.on_event(ev(s, *offset_m(*C, s * 200, 0), vid="VH-000002"))    # driving, ignition on
    assert not [x for x in sigs if x.code == TOW_SUSPECTED]


def test_gps_jump_while_parked_is_not_a_tow():
    det = AnomalyDetector(CFG["anomaly"])
    det.on_event(ev(1, *C, ign=False, speed=0))
    sigs = det.on_event(ev(2, *offset_m(*C, 3000, 0), ign=False, speed=0, flags=["suspect_gps"]))
    assert sigs == []


def test_unflagged_gps_spike_while_parked_is_not_a_tow():
    det = AnomalyDetector(CFG["anomaly"])
    sigs = []
    for s in range(1, 6):
        sigs += det.on_event(ev(s, *C, ign=False, speed=0))
    sigs += det.on_event(ev(6, *offset_m(*C, 1000, 0), ign=False, speed=0))    # 1 km in 20 s = 180 km/h: not flagged
    sigs += det.on_event(ev(7, *C, ign=False, speed=0))                        # and back
    assert not [x for x in sigs if x.code == TOW_SUSPECTED]


def test_device_session_restart_resets_window():
    det = AnomalyDetector(CFG["anomaly"])
    for s in range(1, 6):
        det.on_event(ev(s, *C, ign=False, speed=0))
    far = offset_m(*C, 2000, 0)                              # new session reports from somewhere else
    sigs = [x for s in range(1, 6) for x in det.on_event(ev(s, *far, ts=NOON + 200 + s * 20, ign=False, speed=0))]
    assert not [x for x in sigs if x.code == TOW_SUSPECTED]


def test_glitch_as_oldest_point_in_window_is_not_a_tow_start():
    det = AnomalyDetector(CFG["anomaly"])
    sigs = det.on_event(ev(1, *offset_m(*C, 1000, 0), ign=False, speed=0))    # unflagged 1 km glitch first
    for s in range(2, 25):
        sigs += det.on_event(ev(s, *C, ign=False, speed=0))                    # then parked, for 8 minutes
    assert not [x for x in sigs if x.code == TOW_SUSPECTED]


def test_night_movement_without_baseline_and_suppressed_by_baseline():
    night = NOON - 10 * 3600                                                        # 02:00 local
    det = AnomalyDetector(CFG["anomaly"])
    assert det.on_event(ev(1, *C, ts=night))[0].code == NIGHT_MOVEMENT
    night_owl = AnomalyDetector(CFG["anomaly"], {"VH-000001": Baseline((0, 6), None, None)})
    assert night_owl.on_event(ev(1, *C, ts=night)) == []


def test_tamper_needs_precursor_then_silence():
    det = AnomalyDetector(CFG["anomaly"])
    det.on_event(ev(1, *C, battery=4.1))
    det.on_event(ev(2, *C, battery=3.8))                     # 0.3 V drop -> precursor
    det.on_event(ev(3, *C, ts=NOON + 60, vid="VH-000002"))   # someone else keeps the clock moving
    assert det.tick() == []
    det.on_event(ev(4, *C, ts=NOON + 60 + 1000, vid="VH-000002"))
    assert [s.code for s in det.tick()] == [TAMPER_SUSPECTED]


def test_tamper_cleared_if_vehicle_keeps_reporting():
    det = AnomalyDetector(CFG["anomaly"])
    det.on_event(ev(1, *C, flags=["suspect_gps"]))
    for s in range(2, 60):
        det.on_event(ev(s, *C))
    assert det.tick() == []


def test_speed_spike_uses_baseline():
    det = AnomalyDetector(CFG["anomaly"], {"VH-000001": Baseline((7, 20), 30.0, 5.0)})
    sigs = []
    for s in range(1, 8):
        sigs += det.on_event(ev(s, *offset_m(*C, s * 400, 0), speed=60))
    assert SPEED_SPIKE in {x.code for x in sigs}


# ---- risk scorer ----
def _scorer(dpd=0):
    return RiskScorer(CFG["risk"], {"VH-000001": Loan(1, 1, dpd)})


def _sig(code, ts, strength=1.0):
    return Signal("VH-000001", code, ts, *C, strength)


def test_score_formula_weights_and_loan_multiplier():
    r = _scorer(dpd=45)                                       # m = 1.5
    r.on_signal(_sig(TOW_SUSPECTED, 0))
    d = r.on_signal(_sig(GEOFENCE_EXIT, 0))
    assert d.score == pytest.approx((30 + 25) * 1.5)
    assert d.open_case                                        # 82.5 > 70


def test_score_capped_decays_and_case_opens_once():
    r = _scorer(dpd=180)                                      # m = 2.0 (capped at 90 days)
    for code in (TOW_SUSPECTED, GEOFENCE_EXIT, TAMPER_SUSPECTED):
        d = r.on_signal(_sig(code, 0))
    assert d.score == 100
    assert not r.on_signal(_sig(TOW_SUSPECTED, 10)).open_case
    assert r.score("VH-000001", 43_200) == pytest.approx(80, abs=0.1)   # half-faded after 12 h
    assert r.rescore_all(86_410) == {"VH-000001": 0}


def test_alert_cooldown():
    r = _scorer()
    assert r.on_signal(_sig(TOW_SUSPECTED, 0)).write_alert
    assert not r.on_signal(_sig(TOW_SUSPECTED, 600)).write_alert
    assert r.on_signal(_sig(TOW_SUSPECTED, 1900)).write_alert


def test_engine_towed_out_of_zone_opens_case():
    state = EngineState({1: FENCE}, {"VH-000001": [1]}, {"VH-000001": Loan(7, 1, 60)})
    eng = DetectorEngine(CFG, state)
    scored = []
    start = offset_m(*C, 5500, 0)
    for s in range(1, 30):                                   # towed north, out of the zone, ignition off
        scored += eng.on_event(ev(s, *offset_m(*start, s * 100, 0), ign=False, speed=18))
    codes = {x.signal.code for x in scored}
    assert {TOW_SUSPECTED, GEOFENCE_EXIT} <= codes
    assert sum(x.decision.open_case for x in scored) == 1
