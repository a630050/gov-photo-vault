"""One authorized result set shared by map markers and the photo list.

SQLite development backend: JSON/tag predicates run after scoped SQL filtering.
Production PostGIS and typed projections can replace these predicates.
"""
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
import math
from django.db.models import Q, F
from .models import Dataset, Anomaly, Star, Membership

class SearchError(ValueError):
    pass

def empty(value):
    return value is None or value == '' or value == []

def custom_match(photo, rule):
    key, kind, op = rule['key'], rule['type'], rule['op']
    schema = next((f for f in photo.form_snapshot if f['key'] == key and f.get('type','text') == kind), None)
    value = photo.attrs.get(key) if schema else None
    if op == 'missing':return empty(value)
    if empty(value):return False
    if op == 'contains':return str(rule.get('value','')).casefold() in str(value).casefold()
    if op == 'in':
        values = value if isinstance(value,list) else [value]
        return any(x in rule['values'] for x in values)
    if op == 'equals':return isinstance(value,bool) and value == rule['value']
    if op == 'range':
        try:
            convert = date.fromisoformat if kind == 'date' else float
            actual = convert(value)
            if kind != 'date' and not math.isfinite(actual):return False
            return (empty(rule.get('min')) or actual >= convert(rule['min'])) and (empty(rule.get('max')) or actual <= convert(rule['max']))
        except (ValueError, TypeError, OverflowError):return False
    return False

def search_photos(queryset, user, criteria):
    if not isinstance(criteria,dict):raise SearchError('搜尋條件格式錯誤')
    status = criteria.get('status','submitted')
    if status not in ('submitted','draft','recycled'):raise SearchError('未知照片狀態')
    q = queryset.filter(status=status)
    departments = [int(x) for x in criteria.get('departments',[])]
    datasets = [int(x) for x in criteria.get('datasets',[])]
    if departments or datasets:q=q.filter(Q(dataset__item__department_id__in=departments)|Q(dataset_id__in=datasets))
    if criteria.get('text'):q=q.filter(name__icontains=str(criteria['text'])[:200])
    if criteria.get('star'):q=q.filter(id__in=Star.objects.filter(user=user).values('photo_id'))
    anomaly = criteria.get('anomaly','')
    if anomaly:
        if anomaly not in ('any','none','pending','confirmed','dismissed'):raise SearchError('未知異常狀態')
        cases = Anomaly.objects.all()
        if anomaly not in ('any','none'):cases=cases.filter(state=anomaly)
        q=q.exclude(id__in=cases.values('photo_id')) if anomaly=='none' else q.filter(id__in=cases.values('photo_id'))
    missing_time = q.filter(captured__isnull=True)
    start,end=criteria.get('from'),criteria.get('to')
    try:
        if start and end and date.fromisoformat(start)>date.fromisoformat(end):raise SearchError('拍攝起日不可晚於迄日')
        tz=ZoneInfo('Asia/Taipei')
        if start:q=q.filter(captured__gte=datetime.combine(date.fromisoformat(start),time.min,tzinfo=tz))
        if end:q=q.filter(captured__lt=datetime.combine(date.fromisoformat(end)+timedelta(days=1),time.min,tzinfo=tz))
    except (ValueError,OverflowError) as exc:raise SearchError('拍攝日期範圍不合法') from exc
    rules=criteria.get('fields',[])
    if not isinstance(rules,list) or len(rules)>100:raise SearchError('自訂欄位條件過多')
    if rules:
        if len(datasets)!=1 or departments:raise SearchError('請只選一個資料集再篩選自訂欄位')
        # Only authorized metadata contributes to the accepted field definitions.
        authorized=queryset.filter(dataset_id=datasets[0],status=status)
        definitions={(f['key'],f.get('type','text')) for snapshot in authorized.values_list('form_snapshot',flat=True) for f in snapshot}
        ds=Dataset.objects.select_related('item').filter(id=datasets[0]).first()
        if ds and (user.is_superuser or Membership.objects.filter(user=user,department_id=ds.item.department_id).exists()):
            definitions.update((f['key'],f.get('type','text')) for f in ds.item.fields)
        for rule in rules:
            if not isinstance(rule,dict) or (rule.get('key'),rule.get('type')) not in definitions:raise SearchError('欄位不存在或不屬於此資料集')
            kind,op=rule['type'],rule.get('op')
            allowed={'text':('contains',),'textarea':('contains',),'integer':('range',),'number':('range',),'date':('range',),'select':('in',),'multi':('in',),'boolean':('equals',)}
            if op!='missing' and op not in allowed.get(kind,()):raise SearchError('欄位搜尋方式不合法')
            if op=='equals' and not isinstance(rule.get('value'),bool):raise SearchError('是／否條件不合法')
            if op=='in' and (not isinstance(rule.get('values'),list) or not rule['values']):raise SearchError('請選擇欄位選項')
            if op=='range':
                try:
                    convert=date.fromisoformat if kind=='date' else float
                    bounds=[convert(rule[key]) for key in ('min','max') if not empty(rule.get(key))]
                    if kind!='date' and any(not math.isfinite(v) for v in bounds):raise ValueError()
                    if len(bounds)==2 and bounds[0]>bounds[1]:raise ValueError()
                except (ValueError,TypeError,OverflowError) as exc:raise SearchError('欄位範圍不合法') from exc
    tags=criteria.get('tags',[])
    if not isinstance(tags,list) or any(not isinstance(t,str) for t in tags):raise SearchError('標籤條件格式錯誤')
    rows=[p for p in q.order_by(F('captured').desc(nulls_last=True),'-id') if (not tags or any(t in p.tags for t in tags)) and all(custom_match(p,rule) for rule in rules)]
    return {'ids':[p.id for p in rows],'total':len(rows),'located':sum(p.lat is not None and p.lon is not None for p in rows),'missing_location':sum(p.lat is None or p.lon is None for p in rows),'excluded_missing_time':sum((not tags or any(t in p.tags for t in tags)) and all(custom_match(p,rule) for rule in rules) for p in missing_time) if start or end else 0}
