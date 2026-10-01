from datetime import datetime, timedelta, timezone

import pytest

from simulator.run import demo_offset_s, sim_start

CLOCK = {"offset_h": 0, "time_scale": 1.0, "timezone_offset_h": 5.5}


def test_default_is_real_time():
    assert sim_start(CLOCK, 1_790_000_000.0) == pytest.approx(1_790_000_000.0)


def test_offset_shifts_but_never_rewinds_across_restarts():
    clock = {**CLOCK, "offset_h": -6}
    first, later = sim_start(clock, 1_790_000_000.0), sim_start(clock, 1_790_000_600.0)
    assert first == pytest.approx(1_790_000_000.0 - 6 * 3600)
    assert later - first == pytest.approx(600)          # restart 10 min later -> clock 10 min later


def test_time_scale_counts_from_local_midnight():
    clock = {**CLOCK, "time_scale": 10.0}
    a, b = sim_start(clock, 1_790_000_000.0), sim_start(clock, 1_790_000_060.0)
    assert b - a == pytest.approx(600)                  # 1 real minute = 10 sim minutes, still forward


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=5.5)))


DEMO = {"timezone_offset_h": 5.5, "demo_start_hour": 8, "demo_window_h": 3}


def test_demo_mode_jumps_forward_to_rush_hour():
    base = datetime(2026, 9, 30, 23, 0, tzinfo=timezone(timedelta(hours=5.5))).timestamp()
    off = demo_offset_s(DEMO, base, 0.0)
    assert off > 0 and _local(base + off).hour == 8


def test_demo_mode_restart_never_goes_back():
    base = datetime(2026, 9, 30, 23, 0, tzinfo=timezone(timedelta(hours=5.5))).timestamp()
    off = demo_offset_s(DEMO, base, 0.0)
    assert demo_offset_s(DEMO, base + 1800, off) == off          # restart 30 min later: same clock
    later = demo_offset_s(DEMO, base + 5 * 3600, off)           # past the window: next day's 08:00
    assert later > off and _local(base + 5 * 3600 + later).hour == 8


def test_demo_mode_off():
    assert demo_offset_s({"timezone_offset_h": 5.5, "demo_start_hour": None}, 1_790_000_000.0, 12.0) == 12.0
