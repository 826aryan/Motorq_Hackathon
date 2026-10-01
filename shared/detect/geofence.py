"""Geofence detector - geohash (SPEC §4.1).

Setup: each lender geofence polygon is covered once by geohash-6 cells (~1.2 x 0.6 km), each marked
INSIDE (whole cell in the polygon) or BORDER (cell crosses the edge).
Per event: look up the first 6 chars of the event's geohash. INSIDE -> ok; not in the set -> outside;
BORDER -> exact point-in-polygon (rare, so cheap).
GEOFENCE_EXIT only after N outside events in a row, so GPS noise at the border doesn't flap.
"""
from shared.detect.signals import GEOFENCE_EXIT, Signal
from shared.geo import geohash_bbox, geohash_encode, point_in_polygon
from shared.schema import CanonicalEvent

INSIDE, BORDER = "inside", "border"


def cover(polygon: list[tuple[float, float]], precision: int = 6) -> dict[str, str]:
    """Geohash cells overlapping the polygon -> INSIDE or BORDER.

    A cell is INSIDE when all four corners are inside and no polygon vertex falls inside the cell
    (for the convex zones we use, that means the whole cell is inside). Otherwise it is BORDER if its
    centre, a corner, or a polygon vertex touches it.
    """
    lats = [p[0] for p in polygon]
    lons = [p[1] for p in polygon]
    lat_lo, lat_hi, lon_lo, lon_hi = geohash_bbox(geohash_encode(min(lats), min(lons), precision))
    dlat, dlon = lat_hi - lat_lo, lon_hi - lon_lo
    cells: dict[str, str] = {}
    lat = lat_lo + dlat / 2
    while lat - dlat / 2 <= max(lats):
        lon = lon_lo + dlon / 2
        while lon - dlon / 2 <= max(lons):
            gh = geohash_encode(lat, lon, precision)
            b = geohash_bbox(gh)
            corners = [(b[0], b[2]), (b[0], b[3]), (b[1], b[2]), (b[1], b[3])]
            corners_in = [point_in_polygon(la, lo, polygon) for la, lo in corners]
            vertex_in_cell = any(b[0] <= la <= b[1] and b[2] <= lo <= b[3] for la, lo in polygon)
            if all(corners_in) and not vertex_in_cell:
                cells[gh] = INSIDE
            elif any(corners_in) or vertex_in_cell or point_in_polygon(lat, lon, polygon):
                cells[gh] = BORDER
            lon += dlon
        lat += dlat
    return cells


class GeofenceIndex:
    def __init__(self, polygons: dict[int, list[tuple[float, float]]], precision: int = 6):
        self.precision = precision
        self.polygons = polygons
        self.cells = {fid: cover(poly, precision) for fid, poly in polygons.items()}
        self.exact_checks = 0

    def contains(self, fid: int, lat: float, lon: float, geohash: str) -> bool:
        state = self.cells[fid].get(geohash[: self.precision])
        if state is None:
            return False
        if state == INSIDE:
            return True
        self.exact_checks += 1
        return point_in_polygon(lat, lon, self.polygons[fid])


class GeofenceDetector:
    def __init__(self, index: GeofenceIndex, vehicle_fences: dict[str, list[int]], exit_after: int = 3):
        self.index = index
        self.vehicle_fences = vehicle_fences
        self.exit_after = exit_after
        self.outside_run: dict[tuple[str, int], int] = {}

    def on_event(self, e: CanonicalEvent) -> list[Signal]:
        if "suspect_gps" in e.flags:        # a GPS jump is not a real position: don't count it either way
            return []
        signals = []
        for fid in self.vehicle_fences.get(e.vehicle_id, ()):
            key = (e.vehicle_id, fid)
            if self.index.contains(fid, e.lat, e.lon, e.geohash):
                self.outside_run.pop(key, None)
                continue
            run = self.outside_run.get(key, 0) + 1
            self.outside_run[key] = run
            if run == self.exit_after:      # fire once per exit, not on every event outside
                signals.append(Signal(e.vehicle_id, GEOFENCE_EXIT, e.device_ts, e.lat, e.lon,
                                      detail={"geofence_id": fid}))
        return signals
