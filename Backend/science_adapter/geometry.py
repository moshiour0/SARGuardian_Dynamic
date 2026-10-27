"""Dependency-free input contract shared by the API and isolated science worker.

Only simple, closed, two-dimensional EPSG:4326 polygons are supported. Holes,
multipart geometry and antimeridian crossings are explicit unsupported inputs.
No geometry repair, rounding, bounding-box substitution or default AOI is used.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass

SCIENCE_COMMIT = "10639a916c9829850775410b3ed7de62e527770d"
MAX_AOI_AREA_KM2 = 1_000
MAX_AOI_VERTICES = 1_000


@dataclass(frozen=True)
class ValidatedAOI:
    wkt: str
    sha256: str
    area_km2: float
    coordinates: tuple[tuple[float, float], ...]


def _orientation(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, p) -> bool:
    return min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])


def _intersects(a, b, c, d) -> bool:
    turns = (_orientation(a, b, c), _orientation(a, b, d), _orientation(c, d, a), _orientation(c, d, b))
    if ((turns[0] > 0 and turns[1] < 0) or (turns[0] < 0 and turns[1] > 0)) and ((turns[2] > 0 and turns[3] < 0) or (turns[2] < 0 and turns[3] > 0)):
        return True
    return any(turn == 0 and _on_segment(x, y, point) for turn, x, y, point in (
        (turns[0], a, b, c), (turns[1], a, b, d), (turns[2], c, d, a), (turns[3], c, d, b),
    ))


def validate_wkt(wkt: str, *, max_area_km2: float = MAX_AOI_AREA_KM2, max_vertices: int = MAX_AOI_VERTICES) -> ValidatedAOI:
    if not isinstance(wkt, str) or len(wkt) > 100_000:
        raise ValueError("AOI must be a bounded single-ring EPSG:4326 POLYGON WKT.")
    match = re.fullmatch(r"(?is)\s*POLYGON\s*\(\s*\(([^()]*)\)\s*\)\s*", wkt)
    if not match:
        raise ValueError("AOI must be a single-ring, two-dimensional EPSG:4326 POLYGON; holes and multipart geometry are unsupported.")
    points = []
    for coordinate in match.group(1).split(","):
        parts = coordinate.split()
        if len(parts) != 2:
            raise ValueError("AOI coordinates must be longitude/latitude pairs.")
        try:
            lon, lat = (float(part) for part in parts)
        except ValueError:
            raise ValueError("AOI coordinates must be numeric.") from None
        if not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise ValueError("AOI coordinates must be finite WGS84 longitude/latitude values.")
        points.append((lon or 0.0, lat or 0.0))
    if len(points) < 4 or points[0] != points[-1]:
        raise ValueError("AOI polygon must be closed by repeating its first coordinate.")
    vertices = points[:-1]
    if len(vertices) > max_vertices:
        raise ValueError(f"AOI cannot exceed {max_vertices} vertices.")
    if len(set(vertices)) != len(vertices) or len(set(vertices)) < 3:
        raise ValueError("AOI requires at least three distinct vertices and must not repeat interior vertices.")
    if max(point[0] for point in vertices) - min(point[0] for point in vertices) > 180:
        raise ValueError("Antimeridian-crossing AOIs are unsupported.")
    edges = list(zip(points, points[1:]))
    for index, (a, b) in enumerate(edges):
        previous = vertices[(index - 1) % len(vertices)]
        if _orientation(previous, a, b) == 0 and ((previous[0] - a[0]) * (b[0] - a[0]) + (previous[1] - a[1]) * (b[1] - a[1])) > 0:
            raise ValueError("AOI edges must not overlap.")
        for other_index in range(index + 1, len(edges)):
            if other_index == index + 1 or (index == 0 and other_index == len(edges) - 1):
                continue
            if _intersects(a, b, *edges[other_index]):
                raise ValueError("AOI polygon must not self-intersect or have touching nonadjacent edges.")
    area = abs(math.fsum(math.radians(b[0] - a[0]) * (math.sin(math.radians(a[1])) + math.sin(math.radians(b[1]))) for a, b in edges)) * 6_371_000**2 / 2 / 1_000_000
    if not math.isfinite(area) or area <= 0:
        raise ValueError("AOI must have positive area.")
    if area > max_area_km2:
        raise ValueError(f"AOI exceeds the {max_area_km2:g} km2 maximum.")
    # Ring direction and start vertex do not change geometry or its cache key.
    start = min(range(len(vertices)), key=vertices.__getitem__)
    forward = vertices[start:] + vertices[:start]
    reverse = [forward[0], *reversed(forward[1:])]
    ring = min(forward, reverse)
    closed = tuple([*ring, ring[0]])
    canonical = "POLYGON((" + ", ".join(f"{repr(lon)} {repr(lat)}" for lon, lat in closed) + "))"
    return ValidatedAOI(canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest(), area, closed)


def point_in_polygon(longitude: float, latitude: float, aoi: ValidatedAOI) -> bool:
    """Return whether a WGS84 point lies inside or on the boundary of this AOI."""
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return False
    inside = False
    ring = aoi.coordinates
    for index in range(len(ring) - 1):
        x1, y1 = ring[index]
        x2, y2 = ring[index + 1]
        cross = (longitude - x1) * (y2 - y1) - (latitude - y1) * (x2 - x1)
        on_segment = (
            abs(cross) <= 1e-10
            and min(x1, x2) - 1e-10 <= longitude <= max(x1, x2) + 1e-10
            and min(y1, y2) - 1e-10 <= latitude <= max(y1, y2) + 1e-10
        )
        if on_segment:
            return True
        if (y1 > latitude) != (y2 > latitude):
            crossing_x = (x2 - x1) * (latitude - y1) / (y2 - y1) + x1
            if longitude < crossing_x:
                inside = not inside
    return inside


def processing_fingerprint(*, aoi_wkt: str, start_date: str, end_date: str,
                           product_type: str = "BOTH", acquisition_window: str = "secondary-acquisition-in-window",
                           retention_mode: str = "derived-only", processing_profile: str = "validation-pilot",
                           place_name: str | None = None, place_latitude: float | None = None,
                           place_longitude: float | None = None, target_latitude: float | None = None,
                           target_longitude: float | None = None) -> str:
    payload = {
        "aoi_sha256": validate_wkt(aoi_wkt).sha256, "start_date": str(start_date), "end_date": str(end_date),
        "product_type": product_type, "acquisition_window": acquisition_window,
        "retention_mode": retention_mode, "processing_profile": processing_profile,
        "place_name": place_name, "place_latitude": place_latitude, "place_longitude": place_longitude,
        "target_latitude": target_latitude, "target_longitude": target_longitude,
        "science_commit": SCIENCE_COMMIT, "output_contract": 3,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
