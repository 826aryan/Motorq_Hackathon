from pipeline.reorder import ReorderBuffer
from shared.schema import CanonicalEvent


def ev(seq, vid="VH-000001"):
    return CanonicalEvent(f"{vid}-{seq}", vid, "A", seq, 1000.0 + seq * 20, 0.0, 12.97, 77.59,
                          "tdr1w00", 0.0, 0.0, False, 4.1, 1.0)


def seqs(events):
    return [e.seq for e in events]


def test_in_order_events_released_immediately():
    rb = ReorderBuffer()
    assert seqs(rb.push(ev(1), 0)[0]) == [1]
    assert seqs(rb.push(ev(2), 0)[0]) == [2]


def test_gap_holds_until_missing_event_arrives_then_releases_in_order():
    rb = ReorderBuffer()
    rb.push(ev(1), 0)
    assert rb.push(ev(3), 0)[0] == []
    assert rb.push(ev(4), 0)[0] == []
    assert seqs(rb.push(ev(2), 1)[0]) == [2, 3, 4]


def test_watermark_releases_past_a_gap_that_never_fills():
    rb = ReorderBuffer(watermark_s=120)
    rb.push(ev(1), 0)
    rb.push(ev(3), 0)                       # seq 2 lost (e.g. malformed -> dead letter)
    released = []
    for s in range(4, 12):                  # event time moves on; 3 falls behind the watermark
        released += rb.push(ev(s), 0)[0]
    assert seqs(released)[:2] == [3, 4]
    assert seqs(released) == sorted(seqs(released))


def test_event_older_than_released_goes_to_late():
    rb = ReorderBuffer()
    rb.push(ev(1), 0)
    rb.push(ev(3), 0)
    for s in range(4, 12):
        rb.push(ev(s), 0)
    released, late = rb.push(ev(2), 0)     # arrives after 3.. were already released
    assert released == []
    assert seqs(late) == [2]


def test_silent_vehicle_flushed_by_processing_time():
    rb = ReorderBuffer(max_hold_s=130)
    rb.push(ev(1), 0)
    rb.push(ev(3), 0)
    assert rb.flush_expired(now=100) == []
    assert seqs(rb.flush_expired(now=131)) == [3]


def test_first_event_delayed_still_comes_out_first():
    rb = ReorderBuffer()
    assert rb.push(ev(2), 0)[0] == []                 # seq 1 might still be in flight
    assert seqs(rb.push(ev(1), 5)[0]) == [1, 2]


def test_stream_joined_midway_waits_for_watermark_once():
    rb = ReorderBuffer(watermark_s=120)
    rb.push(ev(50), 0)                                # pipeline restarted; this vehicle is on seq 50
    released = []
    for s in range(51, 60):
        released += rb.push(ev(s), 0)[0]
    assert seqs(released) == list(range(50, 60))[: len(released)]
    assert len(released) >= 3


def test_device_restart_seq_1_starts_new_session_even_if_clock_went_back():
    rb = ReorderBuffer()
    for s in range(1, 80):
        rb.push(ev(s), 0)
    restarted = CanonicalEvent("r1", "VH-000001", "A", 1, 900.0, 0.0, 12.97, 77.59, "tdr1w00",
                               0.0, 0.0, False, 4.1, 1.0)            # seq 1 again, older device_ts
    released, late = rb.push(restarted, 0)
    assert late == [] and seqs(released) == [1]
    assert rb.sessions_restarted == 1


def test_device_restart_detected_even_when_new_seq_1_is_lost():
    rb = ReorderBuffer()
    for s in range(1, 80):
        rb.push(ev(s), 0)
    seq2 = CanonicalEvent("r2", "VH-000001", "A", 2, 900.0, 0.0, 12.97, 77.59, "tdr1w00",
                          0.0, 0.0, False, 4.1, 1.0)               # seq 1 of the new session never arrived
    _, late = rb.push(seq2, 0)
    assert late == [] and rb.sessions_restarted == 1


def test_restart_detected_while_old_events_are_still_held():
    rb = ReorderBuffer()
    for s in range(60, 65):                          # pipeline joined mid-session: all still held (80 s span)
        rb.push(ev(s), 0)
    assert rb.v["VH-000001"].last_seq is None
    new = CanonicalEvent("n1", "VH-000001", "A", 1, 900.0, 0.0, 12.97, 77.59, "tdr1w00",
                         0.0, 0.0, False, 4.1, 1.0)
    released, late = rb.push(new, 0)
    assert late == [] and seqs(released) == [*range(60, 65), 1]   # old session flushed in order first


def test_vehicles_are_independent():
    rb = ReorderBuffer()
    rb.push(ev(1, "VH-000001"), 0)
    rb.push(ev(5, "VH-000001"), 0)
    assert seqs(rb.push(ev(1, "VH-000002"), 0)[0]) == [1]


def test_flush_all_empties_in_order():
    rb = ReorderBuffer()
    rb.push(ev(1), 0)
    for s in (5, 3, 4):
        rb.push(ev(s), 0)
    assert seqs(rb.flush_all()) == [3, 4, 5]
    assert rb.held() == 0
