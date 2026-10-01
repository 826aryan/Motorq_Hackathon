import json

import pytest

from simulator.encoders import STATUS_IGNITION, Reading, encode_a, encode_b, encode_c

R = Reading("VH-004213", 882, 1759049123.456, 12.971598, 77.594566, 50.0, 90.0, True, 4.1, 12345.6)


def test_oem_a_flat_json_kmh_iso():
    d = json.loads(encode_a(R))
    assert d["vehicle_id"] == "VH-004213" and d["speed_kmh"] == 50.0
    assert d["timestamp"].endswith("Z") and d["timestamp"].startswith("2025-09-28T08:45:23.456")


def test_oem_b_pipe_fixed_order_mph_epoch_ms():
    parts = encode_b(R).split("|")
    assert parts[:5] == ["B", "VH-004213", "1759049123456", "12.971598", "77.594566"]
    assert float(parts[5]) == pytest.approx(50 / 1.609344, abs=0.05)
    assert parts[6] == "1" and parts[7] == "882" and len(parts) == 11


def test_oem_c_nested_hex_ms_local_time():
    d = json.loads(encode_c(R, 5.5))
    assert d["dev"] == {"id": "VH-004213", "seq": 882}
    assert d["gps"]["lat_e6"] == 12971598 and d["gps"]["lon_e6"] == 77594566
    assert d["motion"]["spd_ms"] == pytest.approx(50 / 3.6, abs=0.01)
    assert int(d["status"], 16) & STATUS_IGNITION
    assert d["time"] == {"local": "2025-09-28T14:15:23.456", "offset": "+05:30"}


def test_ignition_off_clears_status_bit():
    off = Reading(**{**R.__dict__, "ignition": False})
    assert not int(json.loads(encode_c(off))["status"], 16) & STATUS_IGNITION
