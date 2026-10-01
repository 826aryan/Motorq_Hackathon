import json

import pytest

from pipeline.parser import ParseError, parse
from shared.geo import geohash_encode
from simulator.encoders import Reading, encode_a, encode_b, encode_c

R = Reading("VH-004213", 882, 1759049123.456, 12.971598, 77.594566, 50.0, 90.0, True, 4.1, 12345.6)


@pytest.mark.parametrize("encode", [encode_a, encode_b, encode_c])
def test_all_three_oems_parse_to_same_canonical_values(encode):
    e = parse(encode(R).encode(), ingest_ts=1759049125.0)
    assert e.vehicle_id == "VH-004213" and e.seq == 882
    assert e.device_ts == pytest.approx(R.ts, abs=0.001)          # C: local time + offset -> UTC
    assert e.lat == pytest.approx(R.lat, abs=1e-6) and e.lon == pytest.approx(R.lon, abs=1e-6)
    assert e.speed_kmh == pytest.approx(50.0, abs=0.2)             # B mph, C m/s -> km/h
    assert e.odometer_km == pytest.approx(12345.6, abs=0.02)       # B miles, C metres -> km
    assert e.battery_v == pytest.approx(4.1, abs=0.001)
    assert e.ignition is True and e.heading_deg == pytest.approx(90, abs=0.6)
    assert e.geohash == geohash_encode(e.lat, e.lon, 7)
    assert e.ingest_ts == 1759049125.0 and e.flags == []


def test_same_reading_from_any_oem_gets_same_event_id():
    ids = {parse(enc(R), 0).event_id for enc in (encode_a, encode_b, encode_c)}
    assert len(ids) == 1


def test_oem_c_ignition_comes_from_status_bits():
    off = Reading(**{**R.__dict__, "ignition": False})
    assert parse(encode_c(off), 0).ignition is False


def test_spec_example_b_line_is_rejected_as_too_few_fields():
    with pytest.raises(ParseError, match="b_bad_fields"):
        parse(b"B|VH-004213|1759049123456|12.971598|77.594566|31.1|1|882", 0)


def _a_without(key):
    return json.dumps({k: v for k, v in json.loads(encode_a(R)).items() if k != key}).encode()


@pytest.mark.parametrize(("payload", "reason"), [
    (b"hello", "unknown_format"),
    (b"\xff\xfe", "bad_encoding"),
    (b'{"vehicle_id":"VH-000001","seq":1', "bad_json"),
    (encode_b(R).replace("|", ";", 3).encode(), "unknown_format"),
    (encode_b(R).replace("|4100|", "|4x00|").encode(), "b_bad_values"),
    (encode_c(R).replace('"0x07"', '"0xZZ"').encode(), "c_bad_status_hex"),
    (_a_without("seq"), "missing_seq"),
    (encode_a(R).replace('"VH-004213"', '"XX-1"').encode(), "bad_vehicle_id"),
    (encode_a(R).replace('"lat":12.971598', '"lat":123.0').encode(), "gps_out_of_range"),
    (encode_a(R).replace('"seq":882', '"seq":true').encode(), "bad_type_seq"),
])
def test_broken_payloads_raise_with_reason(payload, reason):
    with pytest.raises(ParseError, match=reason):
        parse(payload, 0)
