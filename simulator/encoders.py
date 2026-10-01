"""Turn one simulated reading into its OEM's wire format (SPEC §1). Each OEM is deliberately different.

A: flat JSON, km/h, ISO-8601 UTC
B: pipe text, fixed order, mph, epoch ms:
   B|vehicle_id|epoch_ms|lat|lon|speed_mph|ignition|seq|heading|battery_mv|odometer_mi
C: nested JSON, m/s, local time + offset, GPS as int x 1e6, ignition inside a hex status code
"""
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

KMH_PER_MPH = 1.609344
KM_PER_MI = 1.609344

# OEM C status bits
STATUS_IGNITION = 0x01
STATUS_GPS_FIX = 0x02
STATUS_MOVING = 0x04


@dataclass
class Reading:
    vehicle_id: str
    seq: int
    ts: float            # epoch seconds, UTC
    lat: float
    lon: float
    speed_kmh: float
    heading_deg: float
    ignition: bool
    battery_v: float
    odometer_km: float


def encode_a(r: Reading) -> str:
    return json.dumps({
        "vehicle_id": r.vehicle_id,
        "seq": r.seq,
        "timestamp": datetime.fromtimestamp(r.ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "lat": round(r.lat, 6),
        "lon": round(r.lon, 6),
        "speed_kmh": round(r.speed_kmh, 1),
        "heading_deg": round(r.heading_deg, 1),
        "ignition": r.ignition,
        "battery_v": round(r.battery_v, 2),
        "odometer_km": round(r.odometer_km, 2),
    }, separators=(",", ":"))


def encode_b(r: Reading) -> str:
    return "|".join([
        "B", r.vehicle_id, str(int(r.ts * 1000)), f"{r.lat:.6f}", f"{r.lon:.6f}",
        f"{r.speed_kmh / KMH_PER_MPH:.1f}", "1" if r.ignition else "0", str(r.seq),
        f"{r.heading_deg:.0f}", str(int(r.battery_v * 1000)), f"{r.odometer_km / KM_PER_MI:.2f}",
    ])


def encode_c(r: Reading, tz_offset_h: float = 5.5) -> str:
    tz = timezone(timedelta(hours=tz_offset_h))
    local = datetime.fromtimestamp(r.ts, tz)
    status = STATUS_GPS_FIX | (STATUS_IGNITION if r.ignition else 0) | (STATUS_MOVING if r.speed_kmh > 1 else 0)
    sign = "+" if tz_offset_h >= 0 else "-"
    hh, mm = divmod(int(abs(tz_offset_h) * 60), 60)
    return json.dumps({
        "dev": {"id": r.vehicle_id, "seq": r.seq},
        "time": {"local": local.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3], "offset": f"{sign}{hh:02d}:{mm:02d}"},
        "gps": {"lat_e6": round(r.lat * 1e6), "lon_e6": round(r.lon * 1e6), "hdg": round(r.heading_deg)},
        "motion": {"spd_ms": round(r.speed_kmh / 3.6, 2)},
        "status": f"0x{status:02X}",
        "power": {"batt_mv": int(r.battery_v * 1000)},
        "odo_m": int(r.odometer_km * 1000),
    }, separators=(",", ":"))


ENCODERS = {"A": encode_a, "B": encode_b, "C": encode_c}
