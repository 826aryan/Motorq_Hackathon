import json

import numpy as np
import pytest
import yaml

from shared.geo import haversine_m
from simulator.chaos import Chaos, corrupt
from simulator.encoders import Reading, encode_a, encode_b, encode_c

R = Reading("VH-000001", 1, 1759049123.0, 12.97, 77.59, 30.0, 10.0, True, 4.1, 100.0)
BASE = {"duplicate_rate": 0, "out_of_order_rate": 0, "max_delay_s": 120, "gps_jitter_m": 0,
        "gps_jump_rate": 0, "gps_jump_m": [1000, 5000], "malformed_rate": 0,
        "burst_every_s": 300, "burst_duration_s": 30, "burst_multiplier": 10}


@pytest.fixture
def make_chaos(tmp_path):
    def make(**overrides):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"chaos": {**BASE, **overrides}}))
        return Chaos(str(path), np.random.default_rng(1))
    return make


def test_clean_config_passes_event_once_unchanged(make_chaos):
    out = make_chaos().damage("payload", "A", now_real=100.0)
    assert out == [(100.0, "payload")]


def test_duplicates_resend_one_to_three_extra_copies(make_chaos):
    chaos = make_chaos(duplicate_rate=1.0)
    counts = {len(chaos.damage("p", "A", 0.0)) for _ in range(200)}
    assert counts == {2, 3, 4}


def test_out_of_order_delays_within_limit(make_chaos):
    chaos = make_chaos(out_of_order_rate=1.0)
    delays = [chaos.damage("p", "A", 0.0)[0][0] for _ in range(200)]
    assert 0 < min(delays) and max(delays) <= 120


def test_gps_jump_moves_point_far(make_chaos):
    moved = make_chaos(gps_jump_rate=1.0).add_gps_noise(R)
    assert 1000 <= haversine_m(R.lat, R.lon, moved.lat, moved.lon) <= 5100


def test_burst_multiplier_at_start(make_chaos):
    assert make_chaos().send_rate_multiplier() == 10


def test_live_reload_picks_up_new_rates(make_chaos, tmp_path):
    chaos = make_chaos()
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"chaos": {**BASE, "duplicate_rate": 1.0}}))
    chaos._mtime = 0   # simulate a changed file
    chaos.reload(force=True)
    assert chaos.cfg["duplicate_rate"] == 1.0


@pytest.mark.parametrize("oem,enc", [("A", encode_a), ("B", encode_b), ("C", encode_c)])
def test_corrupt_always_changes_payload(oem, enc):
    rng = np.random.default_rng(3)
    good = enc(R)
    for _ in range(50):
        bad = corrupt(good, oem, rng)
        assert bad != good and "\n" not in bad


def test_corrupt_c_can_produce_bad_hex():
    rng = np.random.default_rng(0)
    outs = [corrupt(encode_c(R), "C", rng) for _ in range(60)]
    assert any('"0xZZ"' in o for o in outs)
    assert any(_not_json(o) for o in outs)


def _not_json(s):
    try:
        json.loads(s)
        return False
    except ValueError:
        return True
