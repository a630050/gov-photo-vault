import math
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from .search import search_photos,SearchError

def distance_meters(a,b):
    lat1,lat2=math.radians(a.lat),math.radians(b.lat)
    dlat,dlon=lat2-lat1,math.radians(b.lon-a.lon)
    h=math.sin(dlat/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 6371008.8*2*math.asin(math.sqrt(max(0,min(1,h))))

def nearby_photos(queryset,user,base,data):
    if base.status!='submitted':raise SearchError('請選擇正式照片進行附近比對')
    if base.lat is None or base.lon is None:raise SearchError('基準照片沒有座標，請先補登位置')
    radius=float(data.get('radius',100))
    if not math.isfinite(radius) or not 100<=radius<=1000:raise SearchError('比對距離須為 100 至 1000 公尺')
    result=search_photos(queryset,user,{'status':'submitted','departments':data.get('departments',[]),'tags':data.get('tags',[])})
    candidates=[];missing=0
    for p in queryset.filter(id__in=result['ids']).exclude(id=base.id):
        if data.get('only_dataset') and p.dataset_id!=base.dataset_id:continue
        if p.lat is None or p.lon is None:continue
        dist=distance_meters(base,p)
        if dist>radius+1e-7:continue
        if p.captured is None:missing+=1;continue
        candidates.append((p,dist))
    candidates.sort(key=lambda pair:(pair[0].captured,pair[0].id))
    earliest=candidates[0][0].captured if candidates else None
    latest=candidates[-1][0].captured if candidates else None
    def endpoint(key):
        value=data.get(key)
        if not value:return None
        parsed=parse_datetime(value)
        if not parsed or timezone.is_naive(parsed):raise SearchError('時間範圍須提供含時區的日期時間')
        return parsed
    start,end=endpoint('from'),endpoint('to')
    if start and end and start>end:raise SearchError('起始時間不可晚於結束時間')
    return {'rows':[(p,d) for p,d in candidates if (not start or p.captured>=start) and (not end or p.captured<=end)],'earliest':earliest.isoformat() if earliest else None,'latest':latest.isoformat() if latest else None,'nearby_count':len(candidates),'excluded_missing_time':missing,'radius':radius}
