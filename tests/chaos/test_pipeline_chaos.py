"""Chaos tests (SPEC §10): damaged streams in, clean stream out.

Real OEM encoders + the real chaos injector feed the full pipeline (parse -> dedup -> reorder -> noise).
"""
from collections import defaultdict

import numpy as np
import pytest
import yaml

from pipeline.core import Pipeline
from pipeline.dedup import Deduper
from pipeline.noise import NoiseFilter
from pipeline.reorder import ReorderBuffer
from shared.schema import make_event_id
from simulator.chaos import Chaos
from simulator.encoders import ENCODERS, Reading

T0 = 1_790_000_000.0
OEMS = ["A", "B", "C"]


def make_stream(vehicles=30, events_each=40):
    """(oem, reading) for several vehicles driving north, one event every 20 s."""
    out = []
    for v in range(vehicles):
        for s in range(1, events_each + 1):
            r = Reading(f"VH-{v + 1:06d}", s, T0 + s * 20 + v * 0.3, 12.9 + s * 0.0005, 77.5 + v * 0.01,
                        32.0, 0.0, True, 4.1, 1000 + s * 0.18)
            out.append((OEMS[v % 3], r))
    return out


def run(pipeline, arrivals, batch=50):
    """arrivals = [(arrival_time, payload)]; feed in arrival order, then flush."""
    arrivals = sorted(arrivals, key=lambda a: a[0])
    normalized, late, dead = [], [], []
    for i in range(0, len(arrivals), batch):
        chunk = arrivals[i:i + batch]
        out = pipeline.process([(p.encode(), t) for t, p in chunk], now=chunk[-1][0])
        normalized += out.normalized
        late += out.late
        dead += out.dead
    normalized += pipeline.flush().normalized
    return normalized, late, dead


@pytest.fixture
def pipeline(fake_bloom):
    return Pipeline(Deduper(fake_bloom), ReorderBuffer(watermark_s=120, max_hold_s=130), NoiseFilter())


def expected_ids(stream):
    return {make_event_id(r.vehicle_id, r.ts, r.seq) for _, r in stream}


def assert_in_order_per_vehicle(events):
    by_vehicle = defaultdict(list)
    for e in events:
        by_vehicle[e.vehicle_id].append(e.device_ts)
    for ts in by_vehicle.values():
        assert ts == sorted(ts)


def test_each_event_sent_three_times_is_stored_once(pipeline):
    stream = make_stream()
    arrivals = [(r.ts + k * 0.5, ENCODERS[oem](r)) for oem, r in stream for k in range(3)]
    normalized, late, dead = run(pipeline, arrivals)
    assert sorted(e.event_id for e in normalized) == sorted(expected_ids(stream))
    assert pipeline.counts["duplicates_dropped"] == 2 * len(stream)
    assert late == [] and dead == []


def test_shuffled_events_are_stored_in_order(pipeline):
    stream = make_stream()
    rng = np.random.default_rng(0)
    arrivals = [(r.ts + rng.uniform(0, 100), ENCODERS[oem](r)) for oem, r in stream]  # delays < watermark
    normalized, late, _ = run(pipeline, arrivals)
    assert late == []
    assert {e.event_id for e in normalized} == expected_ids(stream)
    assert_in_order_per_vehicle(normalized)


def test_malformed_payloads_go_to_dead_letter_with_reason(pipeline, tmp_path):
    stream = make_stream(vehicles=9, events_each=20)
    chaos = _chaos(tmp_path, malformed_rate=1.0)
    arrivals = [a for oem, r in stream for a in chaos.damage(ENCODERS[oem](r), oem, r.ts)]
    normalized, _, dead = run(pipeline, arrivals)
    assert len(normalized) + len(dead) == len(stream)
    assert len(dead) >= 0.9 * len(stream)          # a few "missing field" hits land on optional-looking keys
    assert all(b'"reason"' in d for d in dead)


def test_full_chaos_nothing_lost_nothing_doubled_order_kept(pipeline, tmp_path):
    """Spec-level rates, turned up: every valid reading comes out exactly once, in order or as late."""
    stream = make_stream(vehicles=60, events_each=60)
    chaos = _chaos(tmp_path, duplicate_rate=0.2, out_of_order_rate=0.2, malformed_rate=0.02, max_delay_s=120)
    arrivals = []
    for oem, r in stream:
        arrivals += chaos.damage(ENCODERS[oem](r), oem, r.ts)
    normalized, late, dead = run(pipeline, arrivals)

    out_ids = [e.event_id for e in normalized] + [e.event_id for e in late]
    assert len(out_ids) == len(set(out_ids)), "an event came out twice"
    malformed_readings = pipeline.counts["dead_letter"]
    assert len(set(out_ids)) >= len(stream) - malformed_readings
    assert set(out_ids) <= expected_ids(stream)
    assert_in_order_per_vehicle(normalized)
    assert pipeline.counts["duplicates_dropped"] > 0 and len(dead) == malformed_readings


def _chaos(tmp_path, **rates):
    base = {"duplicate_rate": 0, "out_of_order_rate": 0, "max_delay_s": 120, "gps_jitter_m": 0,
            "gps_jump_rate": 0, "gps_jump_m": [1000, 5000], "malformed_rate": 0,
            "burst_every_s": 0, "burst_duration_s": 0, "burst_multiplier": 1}
    path = tmp_path / "chaos.yaml"
    path.write_text(yaml.safe_dump({"chaos": {**base, **rates}}))
    return Chaos(str(path), np.random.default_rng(42))


def test_per_oem_counters_and_samples(fake_bloom):
    """Counters split by OEM, and sampled payloads come out with their canonical form and outcome."""
    pipe = Pipeline(Deduper(fake_bloom), ReorderBuffer(watermark_s=120, max_hold_s=130), NoiseFilter(), sample_every=1)
    stream = make_stream(vehicles=6, events_each=5)
    arrivals = [(r.ts, ENCODERS[oem](r)) for oem, r in stream]
    arrivals.append((arrivals[0][0] + 0.1, arrivals[0][1]))              # a duplicate
    arrivals.append((arrivals[1][0] + 0.1, '{"vehicle_id": broken'))     # a malformed OEM A payload
    normalized, _, _ = run(pipe, arrivals)

    c = pipe.counts
    assert c["in.A"] + c["in.B"] + c["in.C"] + c["in.unknown"] == c["in"]
    assert c["in.A"] == 10 + 1 + 1 and c["in.B"] == 10 and c["in.C"] == 10
    assert sum(c[f"normalized.{o}"] for o in OEMS) == c["normalized"] == len(normalized) == 30
    assert c["duplicates_dropped.A"] == 1 and c["dead_letter.oem.A"] == 1

    outcomes = {(s["oem"], s["outcome"]) for s in pipe.samples}
    assert {("A", "clean"), ("B", "clean"), ("C", "clean"), ("A", "dead_letter")} <= outcomes
    b = next(s for s in pipe.samples if s["oem"] == "B" and s["outcome"] == "clean")
    assert b["raw"].startswith("B|") and b["canonical"]["oem"] == "B"   # raw pipe text -> canonical record
