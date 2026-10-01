from pipeline.dedup import Deduper, RedisBloom


def test_first_seen_kept_repeat_dropped(fake_bloom):
    d = Deduper(fake_bloom, slot_s=600)
    assert d.filter_new(["a", "b"], now=0) == [True, True]
    assert d.filter_new(["a", "c"], now=10) == [False, True]


def test_repeat_inside_one_batch_dropped(fake_bloom):
    assert Deduper(fake_bloom).filter_new(["x", "x", "x"], now=0) == [True, False, False]


def test_duplicate_across_slot_boundary_caught_via_previous_filter(fake_bloom):
    d = Deduper(fake_bloom, slot_s=600)
    d.filter_new(["a"], now=599)
    assert d.filter_new(["a"], now=601) == [False]
    assert fake_bloom.reserved == ["dedup:0", "dedup:1"]


def test_old_filters_forgotten_after_two_slots(fake_bloom):
    d = Deduper(fake_bloom, slot_s=600)
    d.filter_new(["a"], now=0)
    assert d.filter_new(["a"], now=1300) == [True]   # slot 2 only checks slots 1 and 2


class _FakeRedis:
    def __init__(self):
        self.calls = []

    def exists(self, key):
        return 0

    def execute_command(self, *args):
        self.calls.append(args)
        return [1] * (len(args) - 2)


def test_redis_bloom_command_shape():
    r = _FakeRedis()
    b = RedisBloom(r)
    assert b.mexists("dedup:1", ["a"]) == [False]     # missing filter: nothing seen, no command sent
    assert b.madd("dedup:2", ["a", "b"]) == [True, True]
    assert r.calls == [("BF.MADD", "dedup:2", "a", "b")]
