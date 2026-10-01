"""Privacy-safe sharing rules (SPEC §9), applied to everything the partner API returns.

1. Pseudonymize IDs   HMAC(vehicle_id) with a separate key per partner -> partners can't join datasets
2. Coarsen location   geohash cut to 5 chars (~5 km cell) instead of GPS
3. k-anonymity        only cells with at least k vehicles are returned
4. Strip personal     no borrower data; timestamps rounded down to the hour
"""
import hashlib
import hmac
from collections import defaultdict


def partner_key(master_secret: str, partner: str) -> bytes:
    """Derive one key per partner from a master secret."""
    return hmac.new(master_secret.encode(), f"partner:{partner}".encode(), hashlib.sha256).digest()


def pseudonym(key: bytes, vehicle_id: str) -> str:
    return hmac.new(key, vehicle_id.encode(), hashlib.sha256).hexdigest()[:16]


def coarse_cell(geohash: str, precision: int = 5) -> str:
    return geohash[:precision]


def round_to_hour(epoch_s: float) -> int:
    return int(epoch_s // 3600 * 3600)


def cell_insights(vehicles: list[dict], k: int = 10, precision: int = 5) -> list[dict]:
    """vehicles: [{geohash, speed_kmh, risk}] -> per coarse cell aggregates, only cells with >= k vehicles."""
    cells: dict[str, list[dict]] = defaultdict(list)
    for v in vehicles:
        cells[coarse_cell(v["geohash"], precision)].append(v)
    out = []
    for cell, members in sorted(cells.items()):
        if len(members) < k:
            continue
        n = len(members)
        out.append({
            "cell": cell,
            "vehicles": n,
            "moving_share": round(sum(m["speed_kmh"] > 5 for m in members) / n, 3),
            "avg_speed_kmh": round(sum(m["speed_kmh"] for m in members) / n, 1),
            "high_risk_share": round(sum(m.get("risk", 0) > 50 for m in members) / n, 3),
        })
    return out


def anonymize_events(events: list[dict], key: bytes, cell_counts: dict[str, int], k: int = 10,
                     precision: int = 5) -> list[dict]:
    """events: [{vehicle_id, code, ts, geohash}] -> pseudonymous, coarse, hour-rounded; rare cells dropped."""
    out = []
    for e in events:
        cell = coarse_cell(e["geohash"], precision)
        if cell_counts.get(cell, 0) < k:
            continue                     # a lone vehicle in a quiet area could be re-identified
        out.append({"vehicle": pseudonym(key, e["vehicle_id"]), "event": e["code"],
                    "hour": round_to_hour(e["ts"]), "cell": cell})
    return out
