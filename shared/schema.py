"""Canonical event (SPEC §1). The parser produces it; nothing after the parser sees raw OEM formats."""
import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass
class CanonicalEvent:
    event_id: str
    vehicle_id: str
    oem: str
    seq: int
    device_ts: float        # epoch seconds UTC (event time)
    ingest_ts: float        # epoch seconds UTC (processing time)
    lat: float
    lon: float
    geohash: str            # 7 chars, ~150 m
    speed_kmh: float
    heading_deg: float
    ignition: bool
    battery_v: float
    odometer_km: float
    flags: list[str] = field(default_factory=list)

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), separators=(",", ":")).encode()

    @classmethod
    def from_json(cls, raw: bytes | str) -> "CanonicalEvent":
        return cls(**json.loads(raw))


def make_event_id(vehicle_id: str, device_ts: float, seq: int) -> str:
    """hash(vehicle_id, device_ts, seq): identical for every copy of the same reading."""
    key = f"{vehicle_id}|{round(device_ts * 1000)}|{seq}".encode()
    return hashlib.sha1(key, usedforsecurity=False).hexdigest()[:20]
