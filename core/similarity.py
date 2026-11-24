"""User-triggered, resumable local perceptual-hash analysis; never runs on upload."""
import math
from itertools import combinations
from PIL import Image, ImageOps, UnidentifiedImageError
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from .models import Dataset, SimilarityRun, SimilarityPair, Anomaly
from .permissions import allowed

ALGORITHM='dhash64-ahash64-v1'
THRESHOLDS={'dhash':6,'ahash':6}
MAX_PHOTOS=300

def fingerprint(field):
    with field.open('rb') as source, Image.open(source) as original:
        image=ImageOps.exif_transpose(original).convert('L')
        # Both gradient and average luminance structure must agree.
        d=list(image.resize((9,8),Image.Resampling.LANCZOS).getdata())
        a=list(image.resize((8,8),Image.Resampling.LANCZOS).getdata())
        average=sum(a)/64
        def bits(values):return format(sum(int(v)<<i for i,v in enumerate(values)),'016x')
        return {'dhash':bits(d[y*9+x]>d[y*9+x+1] for y in range(8) for x in range(8)),
                'ahash':bits(v>average for v in a)}

def distances(a,b):return {k:(int(a[k],16)^int(b[k],16)).bit_count() for k in THRESHOLDS}

def snapshot(p):
    return {'id':p.id,'version':p.version,'sha':p.sha,'captured':p.captured.isoformat() if p.captured else None,'time_confirmed':p.time_confirmed}

def similarity_api(path,data,u):
    from .helpers import Problem,visible,serialize_photos,audit
    def dataset(id):
        ds=Dataset.objects.select_related('item').filter(id=id).first()
        if not ds or not allowed(u,ds.item.department_id,'analyze'):raise Problem('沒有該資料集的近似分析權限',403)
        return ds
    def run(id,lock=False):
        qs=SimilarityRun.objects.select_for_update() if lock else SimilarityRun.objects
        r=qs.filter(id=id).first()
        if not r:raise Problem('找不到分析紀錄',404)
        dataset(r.dataset_id)
        return r
    def run_data(r):
        current={p.id:p for p in visible(u).filter(id__in=[m['id'] for m in r.members])}
        stale=any(m['id'] not in current or current[m['id']].dataset_id!=r.dataset_id or current[m['id']].status!='submitted' or snapshot(current[m['id']])!=m for m in r.members)
        return dict(id=r.id,dataset=r.dataset_id,actor=r.actor.username,created=r.created.isoformat(),state=r.state,hours=r.hours,algorithm=r.algorithm,thresholds=r.thresholds,total=len(r.members),processed=len(r.fingerprints),failed=sum('error' in x for x in r.fingerprints),pairs=r.pairs.count(),stale=stale,error=r.error)
    if path=='similarity/list':
        ds=dataset(data['dataset'])
        eligible=visible(u).filter(dataset=ds,status='submitted').count()
        return JsonResponse({'eligible':eligible,'limit':MAX_PHOTOS,'thresholds':THRESHOLDS,'algorithm':ALGORITHM,'runs':[run_data(r) for r in SimilarityRun.objects.filter(dataset=ds).select_related('actor').order_by('-id')[:50]]})
    if path=='similarity/start':
        ds=dataset(data['dataset'])
        if ds.archived:raise Problem('資料集已封存，不能發起分析',409)
        hours=data.get('hours',24)
        if isinstance(hours,bool):raise Problem('時間門檻格式錯誤',422)
        hours=float(hours)
        if not math.isfinite(hours) or not 0<=hours<=876000:raise Problem('時間門檻須為 0 至 876000 小時',422)
        with transaction.atomic():
            photos=visible(u).filter(dataset=ds,status='submitted').order_by('id')
            ids=data.get('photos')
            if ids is not None:
                if not isinstance(ids,list) or not ids or any(type(id) is not int for id in ids):raise Problem('請選擇有效照片',422)
                photos=photos.filter(id__in=ids)
                if photos.count()!=len(set(ids)):raise Problem('選取照片含其他資料集或不可用照片',422)
            rows=list(photos[:MAX_PHOTOS+1])
            if not 2<=len(rows)<=MAX_PHOTOS:raise Problem(f'本機單次分析須選 2 至 {MAX_PHOTOS} 張正式照片；請縮小選取範圍',422)
            members=[snapshot(p) for p in rows]
            for existing in SimilarityRun.objects.filter(dataset=ds,actor=u,hours=hours,algorithm=ALGORITHM,state__in=['pending','running']):
                if existing.members==members and existing.thresholds==THRESHOLDS:return JsonResponse(run_data(existing))
            r=SimilarityRun.objects.create(dataset=ds,actor=u,hours=hours,algorithm=ALGORITHM,thresholds=THRESHOLDS,members=members)
            audit(u,'similarity_start',r.id,{'dataset':ds.id,'photos':[p.id for p in rows],'hours':hours,'algorithm':ALGORITHM,'thresholds':THRESHOLDS})
        return JsonResponse(run_data(r))
    if path in ['similarity/step','similarity/cancel']:
        with transaction.atomic():
            r=run(data['run'],True)
            if r.actor_id!=u.id and not u.is_superuser:raise Problem('僅執行者或系統管理者可繼續／取消分析',403)
            if r.state in ['completed','cancelled','failed']:return JsonResponse(run_data(r))
            if path.endswith('cancel'):
                r.state='cancelled';r.save();audit(u,'similarity_cancel',r.id)
                return JsonResponse(run_data(r))
            ds=dataset(r.dataset_id)
            if ds.archived:raise Problem('資料集已封存，請取消此分析',409)
            if r.algorithm!=ALGORITHM:
                r.state='failed';r.error='演算法版本已變更，請重新執行';r.save();return JsonResponse(run_data(r))
            current={p.id:p for p in visible(u).filter(dataset=ds,status='submitted',id__in=[m['id'] for m in r.members])}
            if any(m['id'] not in current or snapshot(current[m['id']])!=m for m in r.members):
                r.state='failed';r.error='分析期間照片資料已變動，請重新執行';r.save();return JsonResponse(run_data(r))
            for m in r.members[len(r.fingerprints):len(r.fingerprints)+10]:
                p=current[m['id']]
                try:entry={'id':p.id,**fingerprint(p.file)}
                except (OSError,ValueError,UnidentifiedImageError,Image.DecompressionBombError):entry={'id':p.id,'error':'無法讀取或解碼原始照片'}
                r.fingerprints.append(entry)
            r.state='running'
            if len(r.fingerprints)==len(r.members):
                pairs=[]
                for a,b in combinations([x for x in r.fingerprints if 'error' not in x],2):
                    score=distances(a,b)
                    if any(score[k]>r.thresholds[k] for k in score):continue
                    pa,pb=current[a['id']],current[b['id']]
                    gap=abs((pa.captured-pb.captured).total_seconds())/3600 if pa.captured and pb.captured else None
                    flag='needs_time' if gap is None or not pa.time_confirmed or not pb.time_confirmed else 'over' if gap>r.hours else 'within'
                    pairs.append(SimilarityPair(run=r,a=pa,b=pb,distances=score,gap_hours=gap,time_state=flag))
                SimilarityPair.objects.bulk_create(pairs)
                r.state='completed';audit(u,'similarity_complete',r.id,{'pairs':len(pairs),'failed':sum('error' in x for x in r.fingerprints)})
            r.save()
            return JsonResponse(run_data(r))
    if path=='similarity/results':
        r=run(data['run']);offset=max(0,int(data.get('offset',0)))
        current={p.id:p for p in visible(u).filter(dataset_id=r.dataset_id,status='submitted',id__in=[m['id'] for m in r.members])}
        serialized={item['id']:item for item in serialize_photos(current.values(),u)}
        members={m['id']:m for m in r.members};pairs=[]
        for p in r.pairs.order_by('id')[offset:offset+50]:
            a,b=current.get(p.a_id),current.get(p.b_id)
            if not a or not b:
                pairs.append({'id':p.id,'unavailable':True});continue
            pairs.append(dict(id=p.id,a=serialized[a.id],b=serialized[b.id],distances=p.distances,gap_hours=p.gap_hours,time_state=p.time_state,review=p.review,events=p.events,stale=snapshot(a)!=members[a.id] or snapshot(b)!=members[b.id]))
        return JsonResponse({'run':run_data(r),'pairs':pairs,'offset':offset,'next':offset+50 if r.pairs.count()>offset+50 else None,'failures':[x for x in r.fingerprints if 'error' in x]})
    if path in ['similarity/pair','similarity/review']:
        with transaction.atomic():
            p=SimilarityPair.objects.select_for_update().filter(id=data['pair']).first()
            if not p:raise Problem('找不到配對',404)
            r=run(p.run_id);ds=dataset(r.dataset_id)
            photos=list(visible(u).filter(id__in=[p.a_id,p.b_id],dataset=ds,status='submitted').order_by('id'))
            if len(photos)!=2:raise Problem('照片已移動、回收或無權查看',409)
            members={m['id']:m for m in r.members};stale=any(snapshot(x)!=members[x.id] for x in photos)
            if path.endswith('pair'):return JsonResponse({'photos':serialize_photos(photos,u),'stale':stale})
            if stale:raise Problem('照片資料已變動，請重新分析後標註',409)
            review=data['review'];note=str(data.get('note','')).strip()
            if review not in ['unreviewed','clear','anomaly'] or len(note)>2000:raise Problem('檢閱狀態或說明無效',422)
            if ds.archived:raise Problem('資料集已封存',409)
            if data.get('events')!=len(p.events):raise Problem('其他人已更新檢閱結果，請重新載入',409)
            anomaly_ids=[]
            if review=='anomaly':
                if not allowed(u,ds.item.department_id,'organize'):raise Problem('沒有人工異常標註權限',403)
                if not note:raise Problem('請填寫人工異常說明',422)
                if p.review=='anomaly':raise Problem('此配對已建立異常，請至照片屬性處理',409)
                for x in photos:
                    anomaly=Anomaly.objects.create(photo=x,author=u,text=f'近似分析 #{r.id}／配對 #{p.id}（照片 {p.a_id}、{p.b_id}）：{note}')
                    anomaly_ids.append(anomaly.id);audit(u,'anomaly',x.id,{'id':anomaly.id,'similarity_pair':p.id})
            if p.review=='anomaly' and review!='anomaly':raise Problem('已建立的異常請至照片屬性確認、排除或重開',409)
            p.review=review;p.events.append({'actor':u.username,'at':timezone.now().isoformat(),'review':review,'note':note,'anomalies':anomaly_ids});p.save()
            audit(u,'similarity_review',p.id,p.events[-1])
            return JsonResponse({'ok':True})
    raise Problem('找不到近似分析操作',404)
