import math
import uuid
from django.db import transaction
from django.db.models import F,Q
from django.http import JsonResponse
from django.utils import timezone
from .models import Photo,PhotoEmbedding,Dataset,TagAssertion
from .permissions import allowed
from .semantic_engine import MODEL_KEY,DIMENSIONS,ModelUnavailable,readiness,text_vector,image_vectors
from .ocr import index_ocr_batch,ocr_coverage,ocr_search
from .ocr_engine import MODEL_KEY as OCR_MODEL_KEY,readiness as ocr_readiness,ModelUnavailable as OcrUnavailable

def valid_vector(vector):
    return isinstance(vector,list) and len(vector)==DIMENSIONS and all(type(x) in (int,float) and math.isfinite(x) for x in vector) and .99<sum(x*x for x in vector)<1.01

def pending_photos(q):
    done=PhotoEmbedding.objects.filter(model_key=MODEL_KEY,source_sha=F('photo__sha')).values('photo_id')
    return q.filter(status='submitted').exclude(id__in=done)

def index_batch(limit=5,retry=False):
    """System indexing, independent of search scope. Retrieval always scopes before ranking."""
    q=Photo.objects.filter(status='submitted').order_by('id')
    if retry:PhotoEmbedding.objects.filter(model_key=MODEL_KEY,state='failed').delete()
    rows=list(pending_photos(q)[:limit])
    if not rows:return 0
    results={r['id']:r for r in image_vectors(rows)}
    with transaction.atomic():
        for p in rows:
            if not Photo.objects.filter(id=p.id,sha=p.sha,status='submitted').exists():continue
            result=results.get(p.id,{})
            vector=result.get('vector',[]);ready=valid_vector(vector)
            PhotoEmbedding.objects.update_or_create(photo=p,model_key=MODEL_KEY,defaults={'source_sha':p.sha,'vector':vector if ready else [],'state':'ready' if ready else 'failed','error':'' if ready else result.get('error','模型向量無效')})
    return len(rows)

def semantic_api(path,data,u):
    from .helpers import visible,serialize_photos,Problem,audit,editable
    def scope():
        ids=data.get('datasets',[])
        if not isinstance(ids,list) or any(type(id) is not int for id in ids):raise Problem('資料集條件格式錯誤',422)
        q=visible(u).filter(status='submitted')
        return q.filter(dataset_id__in=ids) if ids else q
    def coverage(q):
        total=q.count()
        indexed=PhotoEmbedding.objects.filter(model_key=MODEL_KEY,photo_id__in=q.values('id'),source_sha=F('photo__sha'))
        ready=indexed.filter(state='ready').count();failed=indexed.filter(state='failed').count()
        return {'total':total,'indexed':ready,'failed':failed,'pending':total-ready-failed}
    if path=='semantic/status':return JsonResponse({'ready':readiness(),'model':MODEL_KEY,**coverage(scope()),'ocr':{'ready':ocr_readiness(),'model':OCR_MODEL_KEY,**ocr_coverage(scope())}})
    if path=='semantic/ocr_index':
        if not ocr_readiness():raise Problem('離線 OCR 環境尚未就緒',503)
        try:processed=index_ocr_batch(limit=10,retry=bool(data.get('retry')))
        except OcrUnavailable as e:raise Problem(str(e),503)
        return JsonResponse({'ok':True,'processed':processed,'ocr':{'ready':ocr_readiness(),'model':OCR_MODEL_KEY,**ocr_coverage(scope())}})
    if path=='semantic/ocr_search':
        query=data.get('query','')
        if not isinstance(query,str) or not query.strip() or len(query.strip())>100:raise Problem('請輸入 1 至 100 字的圖片文字關鍵字（支援英文與繁體中文）',422)
        limit=int(data.get('limit',200))
        if not 1<=limit<=500:raise Problem('筆數設定無效',422)
        q=scope();info=ocr_coverage(q)
        if not info['indexed']:return JsonResponse({'photos':[],'matches':0,'returned':0,'limited':False,'model':OCR_MODEL_KEY,**info})
        hits,matches=ocr_search(q,query.strip(),limit=limit)
        current={p.id:p for p in scope().filter(id__in=[pid for pid,_ in hits])}
        ordered=[current[pid] for pid,_ in hits if pid in current]
        serialized={item['id']:item for item in serialize_photos(ordered,u)}
        photos=[{**serialized[pid],'ocr_snippet':snippet} for pid,snippet in hits if pid in serialized]
        audit(u,'ocr_search',OCR_MODEL_KEY,{'query_length':len(query),'returned':len(photos)})
        return JsonResponse({'photos':photos,'matches':matches,'returned':len(photos),'limited':matches>limit,'model':OCR_MODEL_KEY,**ocr_coverage(scope())})
    if path=='semantic/search':
        query=data.get('query','')
        if not isinstance(query,str) or not query.strip() or len(query.strip())>100:raise Problem('請輸入 1 至 100 字的中文關鍵字或短句',422)
        minimum=float(data.get('minimum',.20));limit=int(data.get('limit',100))
        if not math.isfinite(minimum) or not -1<=minimum<=1 or not 1<=limit<=500:raise Problem('相近度或筆數設定無效',422)
        # Do not embed queries or scan global vectors for unauthorized/empty scopes.
        q=scope();info=coverage(q)
        if not info['indexed']:return JsonResponse({'photos':[],'matches':0,'returned':0,'limited':False,'model':MODEL_KEY,**info})
        try:vector=text_vector(query.strip())
        except ModelUnavailable as e:raise Problem(str(e),503)
        if not valid_vector(vector):raise Problem('模型向量格式無效',503)
        # Re-evaluate authorization after inference, before touching any ranked candidates.
        q=scope()
        embeddings=PhotoEmbedding.objects.filter(model_key=MODEL_KEY,state='ready',source_sha=F('photo__sha'),photo_id__in=q.values('id'))
        scored=[]
        for entry in embeddings.iterator():
            if not valid_vector(entry.vector):continue
            score=sum(a*b for a,b in zip(vector,entry.vector))
            if score>=minimum:scored.append((entry.photo_id,max(-1,min(1,score))))
        scored.sort(key=lambda pair:(-pair[1],pair[0]));matches=len(scored);top=scored[:limit]
        current={p.id:p for p in scope().filter(id__in=[id for id,_ in top])}
        ordered=[current[id] for id,_ in top if id in current]
        serialized={item['id']:item for item in serialize_photos(ordered,u)}
        photos=[{**serialized[id],'semantic_score':round(score,5)} for id,score in top if id in serialized]
        audit(u,'semantic_search',MODEL_KEY,{'query_length':len(query),'returned':len(photos),'datasets':list(scope().values_list('dataset_id',flat=True).distinct())})
        return JsonResponse({'photos':photos,'matches':matches,'returned':len(photos),'limited':matches>limit,'model':MODEL_KEY,**coverage(scope())})
    if path=='semantic/tag':
        ids=data.get('photos');tags=data.get('tags')
        if not isinstance(ids,list) or not 1<=len(ids)<=500 or any(type(id) is not int for id in ids):raise Problem('請選擇 1 至 500 張照片',422)
        if not isinstance(tags,list) or not 1<=len(tags)<=20 or any(not isinstance(t,str) or not t.strip() or len(t.strip())>60 for t in tags):raise Problem('請輸入 1 至 20 個標籤，每個最多 60 字',422)
        tags=sorted(set(t.strip() for t in tags));ids=set(ids)
        batch=str(uuid.uuid4())
        with transaction.atomic():
            photos=list(visible(u).select_for_update().filter(id__in=ids,status='submitted'))
            if len(photos)!=len(ids):raise Problem('照片權限或狀態已改變，請重新搜尋；本批次未新增',409)
            for p in photos:editable(u,p)
            for p in photos:
                for tag in p.tags:
                    if not TagAssertion.objects.filter(photo=p,tag=tag,active=True).exists():TagAssertion.objects.create(photo=p,tag=tag,batch='legacy',actor=p.uploader)
                for tag in tags:TagAssertion.objects.create(photo=p,tag=tag,batch=batch,actor=u)
                p.tags=sorted(set(p.tags+tags));p.save(update_fields=['tags'])
                audit(u,'tags',p.id,{'batch':batch,'tags':tags,'source':'semantic_selection'})
            audit(u,'semantic_batch_tag',batch,{'photos':sorted(ids),'tags':tags})
        return JsonResponse({'ok':True,'batch':batch,'count':len(photos),'tags':tags})
    raise Problem('找不到內容搜尋操作',404)
