"""Geohash and distance helpers, used by simulator, parser, detectors and privacy API."""
import math

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
EARTH_RADIUS_M = 6_371_000.0


def geohash_encode(lat: float, lon: float, precision: int = 7) -> str:
    """Standard geohash: interleave longitude/latitude bisection bits, 5 bits per base-32 char."""
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    chars, bits, value, even = [], 0, 0, True
    while len(chars) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                value = (value << 1) | 1
                lon_lo = mid
            else:
                value <<= 1
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                value = (value << 1) | 1
                lat_lo = mid
            else:
                value <<= 1
                lat_hi = mid
        even = not even
        bits += 1
        if bits == 5:
            chars.append(_BASE32[value])
            bits, value = 0, 0
    return "".join(chars)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def offset_m(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    """Move a point by a small north/east distance in metres (flat-earth approximation)."""
    dlat = north_m / 111_320.0
    dlon = east_m / (111_320.0 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def geohash_bbox(gh: str) -> tuple[float, float, float, float]:
    """(lat_lo, lat_hi, lon_lo, lon_hi) of a geohash cell: replay the bisections encoded in its bits."""
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    even = True
    for ch in gh:
        value = _BASE32.index(ch)
        for bit in (16, 8, 4, 2, 1):
            if even:
                mid = (lon_lo + lon_hi) / 2
                lon_lo, lon_hi = (mid, lon_hi) if value & bit else (lon_lo, mid)
            else:
                mid = (lat_lo + lat_hi) / 2
                lat_lo, lat_hi = (mid, lat_hi) if value & bit else (lat_lo, mid)
            even = not even
    return lat_lo, lat_hi, lon_lo, lon_hi


def geohash_neighbours(gh: str) -> list[str]:
    """The 8 cells around gh (same precision), found by stepping one cell width from its centre."""
    lat_lo, lat_hi, lon_lo, lon_hi = geohash_bbox(gh)
    dlat, dlon = lat_hi - lat_lo, lon_hi - lon_lo
    clat, clon = (lat_lo + lat_hi) / 2, (lon_lo + lon_hi) / 2
    return [geohash_encode(clat + i * dlat, clon + j * dlon, len(gh))
            for i in (-1, 0, 1) for j in (-1, 0, 1) if (i, j) != (0, 0)]


def point_in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray casting: count how many polygon edges a ray going east from the point crosses."""
    inside = False
    n = len(polygon)
    for i in range(n):
        (la1, lo1), (la2, lo2) = polygon[i], polygon[(i + 1) % n]
        if (la1 > lat) != (la2 > lat):
            cross_lon = lo1 + (lat - la1) * (lo2 - lo1) / (la2 - la1)
            if lon < cross_lon:
                inside = not inside
    return inside


def circle_polygon(lat: float, lon: float, radius_m: float, sides: int = 32) -> list[tuple[float, float]]:
    return [offset_m(lat, lon, radius_m * math.cos(a), radius_m * math.sin(a))
            for a in (2 * math.pi * k / sides for k in range(sides))]
