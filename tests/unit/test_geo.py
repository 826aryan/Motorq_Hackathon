import pytest

from shared.geo import bearing_deg, geohash_encode, haversine_m, offset_m


def test_geohash_known_value():
    # Reference point from the geohash spec (Jutland, Denmark).
    assert geohash_encode(57.64911, 10.40744, 11) == "u4pruydqqvj"


def test_geohash_prefix_is_containing_cell():
    assert geohash_encode(12.9716, 77.5946, 7).startswith(geohash_encode(12.9716, 77.5946, 5))


def test_nearby_points_share_cell_far_points_do_not():
    a = geohash_encode(12.97160, 77.59460, 6)
    assert a == geohash_encode(12.97165, 77.59465, 6)
    assert a != geohash_encode(13.05, 77.70, 6)


def test_haversine_one_degree_latitude():
    assert haversine_m(0, 0, 1, 0) == pytest.approx(111_195, rel=1e-3)


def test_bearing_north_and_east():
    assert bearing_deg(0, 0, 1, 0) == pytest.approx(0)
    assert bearing_deg(0, 0, 0, 1) == pytest.approx(90)


def test_offset_round_trip():
    lat, lon = offset_m(12.97, 77.59, 300, 400)
    assert haversine_m(12.97, 77.59, lat, lon) == pytest.approx(500, rel=1e-2)
