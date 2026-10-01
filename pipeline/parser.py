"""Stage 1 - Parser (SPEC §3): detect OEM by payload shape, regex-extract, convert units, add geohash-7.

Anything that does not match raises ParseError(reason); the caller sends it to dead.letter.
  OEM A  flat JSON, km/h, ISO-8601 UTC
  OEM B  pipe text, 11 fixed fields, mph, epoch ms, battery mV, odometer miles
  OEM C  nested JSON, m/s, local time + offset, GPS int x 1e6, ignition in hex status bits
"""
import json
import re
from datetime import datetime

from shared.geo import geohash_encode
from shared.schema import CanonicalEvent, make_event_id

KMH_PER_MPH = 1.609344
KM_PER_MI = 1.609344
STATUS_IGNITION = 0x01

NUM = r"-?\d+(?:\.\d+)?"
VEHICLE_ID = re.compile(r"^VH-\d{6}$")

# Shape detection: which OEM does this payload look like?
SHAPE_B = re.compile(r"^B\|")
SHAPE_C = re.compile(r'^\{"dev":')
SHAPE_A = re.compile(r'^\{"vehicle_id":')

# OEM B: the whole line in one regex, one named group per fixed field.
OEM_B = re.compile(
    rf"^B\|(?P<vid>VH-\d{{6}})\|(?P<ts_ms>\d{{13}})\|(?P<lat>{NUM})\|(?P<lon>{NUM})\|(?P<mph>{NUM})"
    rf"\|(?P<ign>[01])\|(?P<seq>\d+)\|(?P<hdg>{NUM})\|(?P<batt_mv>\d+)\|(?P<odo_mi>{NUM})$"
)
# Field-level patterns for the JSON formats (key order in JSON is not guaranteed, so we match values).
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
LOCAL_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?$")
TZ_OFFSET = re.compile(r"^[+-]\d{2}:\d{2}$")
HEX_STATUS = re.compile(r"^0x[0-9A-Fa-f]{2}$")


class ParseError(ValueError):
    """reason is a short machine-friendly code, stored with the payload in dead.letter."""


def parse(raw: bytes | str, ingest_ts: float) -> CanonicalEvent:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError as exc:
        raise ParseError("bad_encoding") from exc
    text = text.strip()
    if SHAPE_B.match(text):
        fields = _parse_b(text)
    elif SHAPE_C.match(text):
        fields = _parse_c(_load_json(text))
    elif SHAPE_A.match(text):
        fields = _parse_a(_load_json(text))
    else:
        raise ParseError("unknown_format")
    _check_ranges(fields)
    return CanonicalEvent(
        event_id=make_event_id(fields["vehicle_id"], fields["device_ts"], fields["seq"]),
        ingest_ts=ingest_ts,
        geohash=geohash_encode(fields["lat"], fields["lon"], 7),
        flags=[],
        **fields,
    )


def _parse_b(text: str) -> dict:
    m = OEM_B.match(text)
    if not m:
        raise ParseError("b_bad_fields" if text.count("|") != 10 else "b_bad_values")
    return {
        "vehicle_id": m["vid"], "oem": "B", "seq": int(m["seq"]),
        "device_ts": int(m["ts_ms"]) / 1000,
        "lat": float(m["lat"]), "lon": float(m["lon"]),
        "speed_kmh": float(m["mph"]) * KMH_PER_MPH,
        "heading_deg": float(m["hdg"]) % 360,
        "ignition": m["ign"] == "1",
        "battery_v": int(m["batt_mv"]) / 1000,
        "odometer_km": float(m["odo_mi"]) * KM_PER_MI,
    }


def _parse_a(d: dict) -> dict:
    ts = _get(d, "timestamp", str)
    if not ISO_UTC.match(ts):
        raise ParseError("a_bad_timestamp")
    return {
        "vehicle_id": _vehicle_id(_get(d, "vehicle_id", str)), "oem": "A", "seq": _get(d, "seq", int),
        "device_ts": datetime.fromisoformat(ts).timestamp(),
        "lat": _num(d, "lat"), "lon": _num(d, "lon"),
        "speed_kmh": _num(d, "speed_kmh"),
        "heading_deg": _num(d, "heading_deg") % 360,
        "ignition": _get(d, "ignition", bool),
        "battery_v": _num(d, "battery_v"),
        "odometer_km": _num(d, "odometer_km"),
    }


def _parse_c(d: dict) -> dict:
    dev, t, gps = _get(d, "dev", dict), _get(d, "time", dict), _get(d, "gps", dict)
    local, offset = _get(t, "local", str), _get(t, "offset", str)
    if not LOCAL_TS.match(local) or not TZ_OFFSET.match(offset):
        raise ParseError("c_bad_time")
    status = _get(d, "status", str)
    if not HEX_STATUS.match(status):
        raise ParseError("c_bad_status_hex")
    return {
        "vehicle_id": _vehicle_id(_get(dev, "id", str)), "oem": "C", "seq": _get(dev, "seq", int),
        "device_ts": datetime.fromisoformat(local + offset).timestamp(),
        "lat": _get(gps, "lat_e6", int) / 1e6, "lon": _get(gps, "lon_e6", int) / 1e6,
        "speed_kmh": _num(_get(d, "motion", dict), "spd_ms") * 3.6,
        "heading_deg": _num(gps, "hdg") % 360,
        "ignition": bool(int(status, 16) & STATUS_IGNITION),
        "battery_v": _get(_get(d, "power", dict), "batt_mv", int) / 1000,
        "odometer_km": _get(d, "odo_m", int) / 1000,
    }


def _load_json(text: str) -> dict:
    try:
        d = json.loads(text)
    except ValueError as exc:
        raise ParseError("bad_json") from exc
    if not isinstance(d, dict):
        raise ParseError("bad_json")
    return d


def _get(d: dict, key: str, typ: type):
    if key not in d:
        raise ParseError(f"missing_{key}")
    value = d[key]
    # bool is a subclass of int in Python; don't let True pass as a sequence number.
    if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
        raise ParseError(f"bad_type_{key}")
    return value


def _num(d: dict, key: str) -> float:
    if key not in d:
        raise ParseError(f"missing_{key}")
    value = d[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ParseError(f"bad_type_{key}")
    return float(value)


def _vehicle_id(value: str) -> str:
    if not VEHICLE_ID.match(value):
        raise ParseError("bad_vehicle_id")
    return value


def _check_ranges(f: dict) -> None:
    if not (-90 <= f["lat"] <= 90 and -180 <= f["lon"] <= 180):
        raise ParseError("gps_out_of_range")
    if not (0 <= f["speed_kmh"] < 400):
        raise ParseError("speed_out_of_range")
    if f["seq"] < 0:
        raise ParseError("bad_seq")
