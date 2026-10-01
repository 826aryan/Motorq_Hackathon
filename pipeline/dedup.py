"""Stage 2 - Dedup (SPEC §3): RedisBloom filter on event_id.

A new filter every 10 minutes (key dedup:{slot}); each event is checked against the current AND
previous filter, because duplicates arrive within minutes. Filters expire after 20 minutes.
One batch = two Redis round trips (BF.MEXISTS on previous, BF.MADD on current).
"""
from typing import Protocol

from redis.exceptions import ResponseError


class BloomBackend(Protocol):
    def reserve(self, key: str, error_rate: float, capacity: int, ttl_s: int) -> None: ...
    def mexists(self, key: str, items: list[str]) -> list[bool]: ...
    def madd(self, key: str, items: list[str]) -> list[bool]: ...   # True = newly added


class RedisBloom:
    def __init__(self, client):
        self.r = client

    def reserve(self, key, error_rate, capacity, ttl_s):
        pipe = self.r.pipeline()
        pipe.execute_command("BF.RESERVE", key, error_rate, capacity)
        pipe.expire(key, ttl_s)
        try:
            pipe.execute()
        except ResponseError as exc:   # "item exists" when another worker created it first
            if "exists" not in str(exc).lower():
                raise

    def mexists(self, key, items):
        if not self.r.exists(key):
            return [False] * len(items)
        return [bool(x) for x in self.r.execute_command("BF.MEXISTS", key, *items)]

    def madd(self, key, items):
        return [bool(x) for x in self.r.execute_command("BF.MADD", key, *items)]


class Deduper:
    def __init__(self, backend: BloomBackend, slot_s: int = 600, ttl_s: int = 1200,
                 error_rate: float = 0.001, capacity: int = 5_000_000):
        self.b = backend
        self.slot_s, self.ttl_s = slot_s, ttl_s
        self.error_rate, self.capacity = error_rate, capacity
        self._ready_slot: int | None = None

    def key(self, slot: int) -> str:
        return f"dedup:{slot}"

    def filter_new(self, event_ids: list[str], now: float) -> list[bool]:
        """For each id: True if never seen before (keep), False if a duplicate (drop)."""
        if not event_ids:
            return []
        slot = int(now // self.slot_s)
        if self._ready_slot != slot:
            self.b.reserve(self.key(slot), self.error_rate, self.capacity, self.ttl_s)
            self._ready_slot = slot
        in_previous = self.b.mexists(self.key(slot - 1), event_ids)
        added_now = self.b.madd(self.key(slot), event_ids)   # sequential: a repeat inside the batch -> False
        return [added and not prev for added, prev in zip(added_now, in_previous, strict=True)]
