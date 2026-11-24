import json
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent / 'data' / 'tw_village.topo.json'

COUNTY_NORMALIZE = {
    '台北市': '臺北市',
    '台中市': '臺中市',
    '台南市': '臺南市',
    '台東縣': '臺東縣',
    '桃園縣': '桃園市',
}

_INDEX = None


def _decode_arc(arcs, transform, index):
    reverse = index < 0
    arc = arcs[~index if reverse else index]
    sx, sy = transform['scale']
    tx, ty = transform['translate']
    x = 0
    y = 0
    pts = []
    for dx, dy in arc:
        x += dx
        y += dy
        pts.append((x * sx + tx, y * sy + ty))
    if reverse:
        pts.reverse()
    return pts


def _decode_ring(arcs, transform, ring_indices):
    coords = []
    for i, arc_idx in enumerate(ring_indices):
        pts = _decode_arc(arcs, transform, arc_idx)
        if i > 0 and pts:
            coords.extend(pts[1:])
        else:
            coords.extend(pts)
    return coords


def _load_index():
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    if not DATA_PATH.exists():
        _INDEX = []
        return _INDEX

    with DATA_PATH.open('r', encoding='utf-8') as f:
        topo = json.load(f)

    arcs = topo['arcs']
    transform = topo.get('transform', {'scale': [1.0, 1.0], 'translate': [0.0, 0.0]})
    obj_key = next(iter(topo['objects']))
    geometries = topo['objects'][obj_key]['geometries']

    entries = []
    for geom in geometries:
        props = geom.get('properties') or {}
        county = (props.get('COUNTYNAME') or '').strip()
        county = COUNTY_NORMALIZE.get(county, county)
        town = (props.get('TOWNNAME') or '').strip()
        village = (props.get('VILLAGENAM') or props.get('VILLNAME') or '').strip()
        if not (county or town or village):
            continue

        gtype = geom.get('type')
        raw_arcs = geom.get('arcs') or []
        polygons = []
        if gtype == 'Polygon':
            polygons = [[_decode_ring(arcs, transform, r) for r in raw_arcs]]
        elif gtype == 'MultiPolygon':
            polygons = [[_decode_ring(arcs, transform, r) for r in poly] for poly in raw_arcs]

        for poly_rings in polygons:
            outer = poly_rings[0] if poly_rings else []
            if len(outer) < 3:
                continue
            xs = [p[0] for p in outer]
            ys = [p[1] for p in outer]
            entries.append((
                min(xs), min(ys), max(xs), max(ys),
                poly_rings,
                county, town, village,
            ))

    _INDEX = entries
    return _INDEX


def _point_in_ring(lon, lat, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-18) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_polygon(lon, lat, poly_rings):
    if not _point_in_ring(lon, lat, poly_rings[0]):
        return False
    for hole in poly_rings[1:]:
        if _point_in_ring(lon, lat, hole):
            return False
    return True


def reverse_geocode(lat, lon):
    """Return (county, town, village) for WGS84 (lat, lon) offline, or ('', '', '')."""
    if lat is None or lon is None:
        return '', '', ''
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return '', '', ''

    # Quick Taiwan bounding box guard (including Kinmen / Matsu / Penghu)
    if not (21.5 <= lat_f <= 26.5 and 118.0 <= lon_f <= 122.5):
        return '', '', ''

    for minx, miny, maxx, maxy, poly_rings, county, town, village in _load_index():
        if minx <= lon_f <= maxx and miny <= lat_f <= maxy:
            if _point_in_polygon(lon_f, lat_f, poly_rings):
                return county, town, village
    return '', '', ''
