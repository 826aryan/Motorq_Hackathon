"""Canonical event JSON -> one tab-separated row for COPY into the telemetry hypertable."""
import time

COLUMNS = ["vehicle_id", "device_ts", "event_id", "oem", "seq", "ingest_ts", "lat", "lon", "geohash",
           "speed_kmh", "heading_deg", "ignition", "battery_v", "odometer_km", "flags"]


def to_row(e: dict, late: bool) -> str:
    flags = e["flags"] + (["late"] if late else [])
    values = [e["vehicle_id"], _ts(e["device_ts"]), e["event_id"], e["oem"], e["seq"], _ts(e["ingest_ts"]),
              e["lat"], e["lon"], e["geohash"], e["speed_kmh"], e["heading_deg"], "t" if e["ignition"] else "f",
              e["battery_v"], e["odometer_km"], "{" + ",".join(flags) + "}"]
    return "\t".join(str(v) for v in values) + "\n"


def _ts(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch)) + f".{int(epoch % 1 * 1e6):06d}+00"
