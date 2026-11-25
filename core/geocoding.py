"""Offline Taiwan administrative boundary lookup with version, checksum, and status tracking."""
import hashlib
import json
import math
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent / 'data' / 'tw_village.topo.json'
DEFAULT_VERSION = 'tw-village-2026-default'

COUNTY_NORMALIZE = {
    '台北市': '臺北市',
    '台中市': '臺中市',
    '台南市': '臺南市',
    '台東縣': '臺東縣',
    '桃園縣': '桃園市',
}

_CACHE = {}


def clear_boundary_cache():
    _CACHE.clear()


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


def _parse_boundary_file(path_obj, expected_sha256=None):
    if not path_obj or not path_obj.is_file():
        raise FileNotFoundError('邊界檔案不存在')
    raw = path_obj.read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and actual_sha.lower() != expected_sha256.lower():
        raise ValueError('邊界檔案校驗碼不相符')

    data = json.loads(raw.decode('utf-8'))
    entries = []

    def add_polygon_rings(poly_rings, props):
        county = (props.get('COUNTYNAME') or props.get('county') or '').strip()
        county = COUNTY_NORMALIZE.get(county, county)
        town = (props.get('TOWNNAME') or props.get('town') or '').strip()
        village = (props.get('VILLAGENAM') or props.get('VILLNAME') or props.get('village') or '').strip()
        if not (county or town or village):
            return
        outer = poly_rings[0] if poly_rings else []
        if len(outer) < 3:
            return
        xs = [p[0] for p in outer]
        ys = [p[1] for p in outer]
        entries.append((min(xs), min(ys), max(xs), max(ys), poly_rings, county, town, village))

    if data.get('type') == 'Topology' and 'arcs' in data and 'objects' in data:
        arcs = data['arcs']
        transform = data.get('transform', {'scale': [1.0, 1.0], 'translate': [0.0, 0.0]})
        obj_key = next(iter(data['objects']))
        for geom in data['objects'][obj_key]['geometries']:
            props = geom.get('properties') or {}
            gtype = geom.get('type')
            raw_arcs = geom.get('arcs') or []
            if gtype == 'Polygon':
                add_polygon_rings([_decode_ring(arcs, transform, r) for r in raw_arcs], props)
            elif gtype == 'MultiPolygon':
                for poly in raw_arcs:
                    add_polygon_rings([_decode_ring(arcs, transform, r) for r in poly], props)
    elif data.get('type') == 'FeatureCollection':
        for feat in data.get('features') or []:
            geom = feat.get('geometry') or {}
            props = feat.get('properties') or {}
            gtype = geom.get('type')
            coords = geom.get('coordinates') or []
            if gtype == 'Polygon':
                add_polygon_rings([[(pt[0], pt[1]) for pt in ring] for ring in coords], props)
            elif gtype == 'MultiPolygon':
                for poly in coords:
                    add_polygon_rings([[(pt[0], pt[1]) for pt in ring] for ring in poly], props)
    else:
        raise ValueError('不支援的邊界檔案格式')

    if not entries:
        raise ValueError('邊界檔案未包含有效多邊形')
    return entries


def load_boundary_index(boundary_path=None, expected_sha256=None):
    path_obj = Path(boundary_path) if boundary_path else DATA_PATH
    key = (str(path_obj.resolve()) if path_obj.exists() else str(path_obj), expected_sha256 or '')
    if key in _CACHE:
        return _CACHE[key]
    entries = _parse_boundary_file(path_obj, expected_sha256=expected_sha256)
    _CACHE[key] = entries
    return entries


def _point_on_segment(px, py, x1, y1, x2, y2, eps=1e-11):
    cross = (px - x1) * (y2 - y1) - (py - y1) * (x2 - x1)
    if abs(cross) > eps:
        return False
    return (min(x1, x2) - eps <= px <= max(x1, x2) + eps) and (min(y1, y2) - eps <= py <= max(y1, y2) + eps)


def _point_in_ring(lon, lat, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if _point_on_segment(lon, lat, xi, yi, xj, yj):
            return True
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-18) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_polygon(lon, lat, poly_rings):
    if not _point_in_ring(lon, lat, poly_rings[0]):
        return False
    for hole in poly_rings[1:]:
        # Interior holes (such as exclaves or inner boundaries) must be excluded,
        # while a point exactly on an outer/inner boundary edge belongs to the outer ring unless inside the hole interior.
        if _point_in_ring(lon, lat, hole):
            # Check if strictly inside hole (not on hole boundary)
            on_hole_edge = False
            n = len(hole)
            j = n - 1
            for i in range(n):
                if _point_on_segment(lon, lat, hole[i][0], hole[i][1], hole[j][0], hole[j][1]):
                    on_hole_edge = True
                    break
                j = i
            if not on_hole_edge:
                return False
    return True


def geocode_with_status(lat, lon, *, boundary_path=None, version_label=None, expected_sha256=None):
    """Reverse geocode WGS84 coordinates and return structured status + metadata."""
    ver = version_label or DEFAULT_VERSION
    if lat is None or lon is None:
        return {'county': '', 'town': '', 'village': '', 'status': 'no_coords', 'version': '', 'error': ''}
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return {'county': '', 'town': '', 'village': '', 'status': 'out_of_bounds', 'version': ver, 'error': '座標格式無效'}

    if not (math.isfinite(lat_f) and math.isfinite(lon_f) and -90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return {'county': '', 'town': '', 'village': '', 'status': 'out_of_bounds', 'version': ver, 'error': '座標超出經緯度範圍'}

    if not (21.5 <= lat_f <= 26.5 and 118.0 <= lon_f <= 122.5):
        return {'county': '', 'town': '', 'village': '', 'status': 'out_of_bounds', 'version': ver, 'error': '座標位於臺灣及離島支援範圍外'}

    try:
        entries = load_boundary_index(boundary_path=boundary_path, expected_sha256=expected_sha256)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return {
            'county': '',
            'town': '',
            'village': '',
            'status': 'boundary_unavailable',
            'version': ver,
            'error': f'行政區邊界資料不可用：{exc}',
        }

    try:
        for minx, miny, maxx, maxy, poly_rings, county, town, village in entries:
            if minx <= lon_f <= maxx and miny <= lat_f <= maxy:
                if _point_in_polygon(lon_f, lat_f, poly_rings):
                    return {
                        'county': county,
                        'town': town,
                        'village': village,
                        'status': 'ok',
                        'version': ver,
                        'error': '',
                    }
        return {
            'county': '',
            'town': '',
            'village': '',
            'status': 'not_found',
            'version': ver,
            'error': '座標在支援範圍內但未落入任何行政區多邊形',
        }
    except Exception as exc:
        return {
            'county': '',
            'town': '',
            'village': '',
            'status': 'failed',
            'version': ver,
            'error': f'行政區反查失敗：{exc}',
        }


def reverse_geocode(lat, lon, *, boundary_path=None, expected_sha256=None):
    """Return (county, town, village) for WGS84 (lat, lon) offline, or ('', '', '')."""
    res = geocode_with_status(lat, lon, boundary_path=boundary_path, expected_sha256=expected_sha256)
    return res['county'], res['town'], res['village']
