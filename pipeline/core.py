"""The four cleaning stages chained together, independent of Kafka so it can be tested directly.

raw payloads -> 1 parse -> 2 dedup -> 3 reorder -> 4 noise -> normalized
                  |           |          |
             dead.letter   dropped   late.events          (everything counted, nothing silently lost)

Counters are kept overall and per OEM (e.g. `in.B`, `normalized.C`). With sample_every > 0, one raw payload in
N is followed through the stages and recorded with its canonical form and outcome (for the Pipeline screen).
"""
import json
from collections import Counter
from dataclasses import asdict, dataclass, field

from pipeline.dedup import Deduper
from pipeline.noise import NoiseFilter
from pipeline.parser import ParseError, parse
from pipeline.reorder import ReorderBuffer
from shared.schema import CanonicalEvent


@dataclass
class Output:
    normalized: list[CanonicalEvent] = field(default_factory=list)
    late: list[CanonicalEvent] = field(default_factory=list)
    dead: list[bytes] = field(default_factory=list)          # JSON {reason, payload, ingest_ts}


def sniff_oem(raw: bytes) -> str:
    """OEM from the payload's shape, also for payloads that fail to parse (same prefixes as the parser)."""
    head = raw.lstrip()[:16]
    if head.startswith(b"B|"):
        return "B"
    if head.startswith(b'{"dev":'):
        return "C"
    if head.startswith(b'{"vehicle_id":'):
        return "A"
    return "unknown"


class Pipeline:
    MAX_TRACKED = 2000          # sampled events still in flight (held in the reorder buffer)

    def __init__(self, deduper: Deduper, reorder: ReorderBuffer, noise: NoiseFilter, sample_every: int = 0):
        self.dedup = deduper
        self.reorder = reorder
        self.noise = noise
        self.counts: Counter = Counter()
        self._next_expiry_check = 0.0
        self.sample_every = sample_every
        self.samples: list[dict] = []                 # finished samples, drained by the runner
        self._tracked: dict[str, dict] = {}           # event_id -> sample waiting for its outcome

    @classmethod
    def from_config(cls, cfg: dict, bloom_backend) -> "Pipeline":
        return cls(Deduper(bloom_backend, **cfg["dedup"]), ReorderBuffer(**cfg["reorder"]), NoiseFilter(**cfg["noise"]),
                   sample_every=cfg.get("sample_every", 0))

    def _sample(self, oem: str, raw: bytes, outcome: str, event: CanonicalEvent | None = None, detail: str = ""):
        self.samples.append({"oem": oem, "raw": raw.decode("utf-8", "replace")[:400], "outcome": outcome,
                             "detail": detail, "canonical": asdict(event) if event else None})

    def process(self, batch: list[tuple[bytes, float]], now: float) -> Output:
        """batch = [(raw payload, ingest_ts)]. `now` is processing time, used for Bloom slots and hold limits."""
        out = Output()
        parsed: list[CanonicalEvent] = []
        for raw, ingest_ts in batch:
            self.counts["in"] += 1
            oem = sniff_oem(raw)
            self.counts[f"in.{oem}"] += 1
            sampled = self.sample_every > 0 and self.counts["in"] % self.sample_every == 0
            try:
                e = parse(raw, ingest_ts)
                parsed.append(e)
                if sampled and len(self._tracked) < self.MAX_TRACKED:
                    self._tracked[e.event_id] = {"oem": oem, "raw": raw}
            except ParseError as exc:
                self.counts["dead_letter"] += 1
                self.counts[f"dead_letter.{exc}"] += 1
                self.counts[f"dead_letter.oem.{oem}"] += 1
                if sampled:
                    self._sample(oem, raw, "dead_letter", detail=str(exc))
                out.dead.append(json.dumps({"reason": str(exc), "payload": raw.decode("utf-8", "replace"),
                                            "ingest_ts": ingest_ts}).encode())

        keep = self.dedup.filter_new([e.event_id for e in parsed], now)
        for e, is_new in zip(parsed, keep, strict=True):
            if not is_new:
                self.counts["duplicates_dropped"] += 1
                self.counts[f"duplicates_dropped.{e.oem}"] += 1
                if (t := self._tracked.pop(e.event_id, None)) is not None:
                    self._sample(t["oem"], t["raw"], "duplicate", e)
                continue
            released, late = self.reorder.push(e, now)
            out.late.extend(late)
            for le in late:
                if (t := self._tracked.pop(le.event_id, None)) is not None:
                    self._sample(t["oem"], t["raw"], "late", le, "more than the watermark behind")
            self._emit(released, out)
        if now >= self._next_expiry_check:     # scanning every vehicle is O(fleet): once a second is plenty
            self._emit(self.reorder.flush_expired(now), out)
            self._next_expiry_check = now + 1.0

        self.counts["late"] += len(out.late)
        return out

    def flush(self) -> Output:
        out = Output()
        self._emit(self.reorder.flush_all(), out)
        return out

    def _emit(self, events: list[CanonicalEvent], out: Output) -> None:
        for e in events:
            e = self.noise.apply(e)
            if e.flags:
                self.counts["suspect_gps"] += 1
            self.counts[f"normalized.{e.oem}"] += 1
            if (t := self._tracked.pop(e.event_id, None)) is not None:
                self._sample(t["oem"], t["raw"], "flagged" if e.flags else "clean", e, ",".join(e.flags))
            out.normalized.append(e)
        self.counts["normalized"] += len(events)
