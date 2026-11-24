from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import math


def extract_metadata(image):
    """Preserve EXIF provenance separately from later user edits."""
    exif = image.getexif()
    try:
        nested = exif.get_ifd(34665)
    except (KeyError, ValueError, TypeError, OSError):
        nested = {}
    stamp = nested.get(36867) or exif.get(36867) or exif.get(306)
    offset = nested.get(36881) or exif.get(36881)
    captured = None
    confirmed = False
    warnings = []
    if stamp:
        try:
            naive = datetime.strptime(str(stamp), '%Y:%m:%d %H:%M:%S')
            tz = ZoneInfo('Asia/Taipei')
            if offset:
                text = str(offset)
                if len(text) != 6 or text[0] not in '+-' or text[3] != ':':
                    raise ValueError('invalid EXIF offset')
                h, m = int(text[1:3]), int(text[4:6])
                if h > 23 or m > 59:
                    raise ValueError('invalid EXIF offset')
                tz = timezone(timedelta(minutes=(h * 60 + m) * (-1 if text[0] == '-' else 1)))
                confirmed = True
            captured = naive.replace(tzinfo=tz)
            if not offset:
                warnings.append('照片未提供時區，暫以臺灣時間解讀，請確認')
        except (ValueError, TypeError, OverflowError):
            warnings.append('拍攝時間無法解析，請人工補登')
    else:
        warnings.append('照片沒有拍攝時間')
    lat = lon = None
    try:
        gps = exif.get_ifd(34853)
        def coord(value):
            return float(value[0]) + float(value[1]) / 60 + float(value[2]) / 3600
        if gps:
            lat = coord(gps[2]) * (-1 if gps[1] in ('S', b'S') else 1)
            lon = coord(gps[4]) * (-1 if gps[3] in ('W', b'W') else 1)
            if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError('invalid GPS')
    except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError, OSError):
        lat = lon = None
    if lat is None:
        warnings.append('照片沒有有效 GPS 座標')
    snapshot = {'captured': captured.isoformat() if captured else None, 'raw_time': str(stamp) if stamp else None,
                'raw_offset': str(offset) if offset else None, 'time_source': 'exif' if captured else 'missing',
                'timezone_inferred': bool(captured and not confirmed), 'lat': lat, 'lon': lon,
                'location_source': 'exif' if lat is not None else 'missing', 'warnings': warnings}
    return captured, confirmed, lat, lon, snapshot
