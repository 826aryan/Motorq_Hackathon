from pipeline.noise import SUSPECT_GPS, NoiseFilter
from shared.geo import offset_m
from shared.schema import CanonicalEvent


def ev(seq, lat, lon, speed=0.0):
    return CanonicalEvent(str(seq), "VH-000001", "A", seq, seq * 20.0, 0.0,
                          lat, lon, "x", speed, 0.0, speed > 0, 4.1, 1.0)


def test_parked_jitter_snapped_to_previous_point():
    nf = NoiseFilter()
    nf.apply(ev(1, 12.97, 77.59))
    e = nf.apply(ev(2, *offset_m(12.97, 77.59, 6, -5)))
    assert (e.lat, e.lon) == (12.97, 77.59)
    assert len(e.geohash) == 7 and nf.snapped == 1


def test_real_movement_not_snapped():
    nf = NoiseFilter()
    nf.apply(ev(1, 12.97, 77.59, speed=30))
    lat, lon = offset_m(12.97, 77.59, 150, 0)
    assert nf.apply(ev(2, lat, lon, speed=30)).lat == lat


def test_gps_jump_flagged_not_dropped_and_return_judged_against_trusted_fix():
    nf = NoiseFilter()
    nf.apply(ev(1, 12.97, 77.59, speed=30))
    jumped = nf.apply(ev(2, *offset_m(12.97, 77.59, 5000, 0), speed=30))   # 5 km in 20 s = 900 km/h
    assert SUSPECT_GPS in jumped.flags
    back = nf.apply(ev(3, *offset_m(12.97, 77.59, 300, 0), speed=30))     # 300 m in 40 s vs trusted fix
    assert back.flags == []
