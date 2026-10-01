"""Stage 3 - Reorder buffer (SPEC §3): per-vehicle min-heap by device_ts, released in event-time order.

Watermark = newest device_ts seen for the vehicle - 2 min. An event older than what we have
already released, or behind the watermark, can no longer go out in order -> late.events.

Refinement over a plain watermark: the device sequence number is contiguous, so when the next
expected seq is at the top of the heap it is released immediately. Events only wait when there is
a real gap (an event still in flight), and never longer than the watermark allows. This keeps live
detection fast (seconds, not 2 minutes) while still guaranteeing order.
"""
import heapq
from dataclasses import dataclass, field

from shared.schema import CanonicalEvent


@dataclass
class _VehicleBuffer:
    heap: list = field(default_factory=list)       # (device_ts, seq, held_since, event)
    max_seen_ts: float = float("-inf")
    last_ts: float = float("-inf")                  # device_ts of the last released event
    last_seq: int | None = None
    max_seq: int = -1                               # highest seq seen, held or released


class ReorderBuffer:
    def __init__(self, watermark_s: float = 120, max_hold_s: float = 130,
                 restart_seq_max: int = 5, restart_min_drop: int = 50):
        self.watermark_s = watermark_s
        self.max_hold_s = max_hold_s
        self.restart_seq_max = restart_seq_max
        self.restart_min_drop = restart_min_drop
        self.v: dict[str, _VehicleBuffer] = {}
        self.sessions_restarted = 0

    def push(self, e: CanonicalEvent, now: float) -> tuple[list[CanonicalEvent], list[CanonicalEvent]]:
        """Add one event. Returns (released in order, late)."""
        b = self.v.setdefault(e.vehicle_id, _VehicleBuffer())
        restarted = []
        if e.seq <= self.restart_seq_max and b.max_seq - e.seq >= self.restart_min_drop:
            # Tracker rebooted (or the simulator restarted): seq, and possibly its clock, start over.
            # "Back to a small seq, far below the highest seen" rather than "seq == 1": the new
            # session's first event may itself be lost, and a merely late event is only a few seqs behind.
            # Hand out what we hold for the old session, in order, then track the new one fresh.
            while b.heap:
                restarted.append(self._pop(b))
            b = self.v[e.vehicle_id] = _VehicleBuffer()
            self.sessions_restarted += 1
        if e.device_ts < b.last_ts or e.device_ts < b.max_seen_ts - self.watermark_s:
            return restarted, [e]
        b.max_seen_ts = max(b.max_seen_ts, e.device_ts)
        b.max_seq = max(b.max_seq, e.seq)
        heapq.heappush(b.heap, (e.device_ts, e.seq, now, e))
        return restarted + self._release(b, now), []

    def flush_expired(self, now: float) -> list[CanonicalEvent]:
        """Release events held too long in processing time (e.g. a vehicle that went silent)."""
        out = []
        for b in self.v.values():
            if b.heap and now - b.heap[0][2] >= self.max_hold_s:
                out.extend(self._release(b, now, force_first=True))
        return out

    def flush_all(self) -> list[CanonicalEvent]:
        """Release everything in order (partition revoked / shutdown)."""
        out = []
        for b in self.v.values():
            while b.heap:
                out.append(self._pop(b))
        return out

    def held(self) -> int:
        return sum(len(b.heap) for b in self.v.values())

    def _release(self, b: _VehicleBuffer, now: float, force_first: bool = False) -> list[CanonicalEvent]:
        out = []
        watermark = b.max_seen_ts - self.watermark_s
        while b.heap:
            ts, seq, held_since, _ = b.heap[0]
            # Nothing released yet for this vehicle: only seq 1 is known to be first. Anything else
            # (e.g. after a pipeline restart) waits for the watermark, since an earlier event may be in flight.
            next_in_sequence = seq == 1 if b.last_seq is None else seq == b.last_seq + 1
            if next_in_sequence or ts <= watermark or force_first or now - held_since >= self.max_hold_s:
                out.append(self._pop(b))
                force_first = False
            else:
                break
        return out

    @staticmethod
    def _pop(b: _VehicleBuffer) -> CanonicalEvent:
        ts, seq, _, e = heapq.heappop(b.heap)
        b.last_ts, b.last_seq = ts, seq
        return e
