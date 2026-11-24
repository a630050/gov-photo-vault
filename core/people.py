import io,math,uuid,hashlib
from PIL import Image,ImageOps,UnidentifiedImageError
from django.db import transaction
from django.db.models import F
from django.http import JsonResponse,FileResponse
from django.core import signing
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.contrib.auth import get_user_model
from .models import Person,PersonReference,FacePhotoIndex,FaceObservation,PersonPhotoLink,PersonLinkEvent,Department,Photo
from .permissions import allowed
from .face_engine import MODEL_KEY,DIMENSIONS,MINIMUM,readiness,extract,ModelUnavailable

def valid_vector(v):return isinstance(v,list) and len(v)==DIMENSIONS and all(type(x) in (int,float) and math.isfinite(x) for x in v) and .99<sum(x*x for x in v)<1.01

def authorize(u,department,*capabilities):
    from .views import Problem
    if not get_user_model().objects.filter(id=u.id,is_active=True).exists() or not all(allowed(u,department,k) for k in ('people_view',*capabilities)):raise Problem('未取得此科室的人物閱覽或專用操作權限',403)

def get_person(u,id,*caps,lock=False):
    from .views import Problem
    try:id=uuid.UUID(str(id))
    except ValueError:raise Problem('人物識別格式錯誤',422)
    q=Person.objects.select_for_update() if lock else Person.objects
    person=q.filter(id=id).first()
    if not person:raise Problem('找不到人物或沒有權限',404)
    authorize(u,person.department_id,*caps)
    return person

def photo_people(p,u):
    if p.status!='submitted' or not allowed(u,p.dataset.item.department_id,'people_view'):return []
    return [{'link':x.id,'person':str(x.person_id),'name':x.person.name,'code':x.person.code,'face':str(x.face_id),'bbox':x.face.bbox} for x in PersonPhotoLink.objects.filter(active=True,person__active=True,person__department_id=p.dataset.item.department_id,face__photo=p,face__model_key=MODEL_KEY,face__source_sha=p.sha).select_related('person','face')]

def reference_asset(request,id):
    from .views import Problem
    try:
        if not request.user.is_authenticated:raise Problem('請先登入',401)
        ref=PersonReference.objects.select_related('person').filter(id=id).first()
        if not ref:raise Problem('找不到參考照片',404)
        authorize(request.user,ref.person.department_id)
        if not (allowed(request.user,ref.person.department_id,'people_manage') or allowed(request.user,ref.person.department_id,'face_search')):raise Problem('沒有參考照片閱覽權限',403)
        if (not ref.active or not ref.person.active) and not allowed(request.user,ref.person.department_id,'people_manage'):raise Problem('參考照片已停用',404)
        response=FileResponse(ref.file.open('rb'),content_type='image/jpeg');response['Cache-Control']='private, no-store';return response
    except Problem as e:return JsonResponse({'error':e.message},status=e.status)
    except OSError:return JsonResponse({'error':'參考照片暫不可用'},status=503)

def people_api(request,path,data,u):
    from .views import Problem,visible,serialize,audit
    def person_data(p):
        manage=allowed(u,p.department_id,'people_manage');face_search=allowed(u,p.department_id,'face_search')
        refs=p.references.all() if manage else p.references.filter(active=True)
        return {'id':str(p.id),'department':p.department_id,'code':p.code,'name':p.name,'notes':p.notes,'active':p.active,'version':p.version,'can_manage':manage,'can_search':face_search,'references':[{'id':str(r.id),'active':r.active,'quality':r.quality,'model':r.model_key,'created':r.created.isoformat(),'preview':'/people/reference/'+str(r.id)} for r in refs.order_by('created')] if manage or face_search else [],'reference_count':p.references.filter(active=True).count()}
    def scope(p):
        authorize(u,p.department_id,'face_search')
        q=visible(u).filter(status='submitted',dataset__item__department_id=p.department_id)
        ids=data.get('datasets',[])
        if not isinstance(ids,list) or any(type(x) is not int for x in ids):raise Problem('資料集條件無效',422)
        return q.filter(dataset_id__in=ids) if ids else q
    def coverage(q):
        indexes=FacePhotoIndex.objects.filter(photo_id__in=q.values('id'),model_key=MODEL_KEY,source_sha=F('photo__sha'))
        indexed=indexes.filter(state='ready').count();failed=indexes.filter(state='failed').count();total=q.count()
        return {'total':total,'indexed':indexed,'failed':failed,'pending':total-indexed-failed,'faces':FaceObservation.objects.filter(photo_id__in=q.values('id'),source_sha=F('photo__sha'),model_key=MODEL_KEY).count()}
    if path=='people/list':
        department=int(data['department']);authorize(u,department)
        q=Person.objects.filter(department_id=department)
        if not allowed(u,department,'people_manage'):q=q.filter(active=True)
        return JsonResponse({'people':[person_data(p) for p in q.order_by('code','created')],'model_ready':readiness(),'model':MODEL_KEY,'minimum':MINIMUM})
    if path=='people/save':
        with transaction.atomic():
            if data.get('id'):
                p=get_person(u,data['id'],'people_manage',lock=True)
                if data.get('version')!=p.version:raise Problem('人物資料已更新，請重新載入',409)
            else:
                department=int(data['department']);authorize(u,department,'people_manage')
                if not Department.objects.filter(id=department).exists():raise Problem('找不到科室',404)
                p=Person(department_id=department,creator=u)
            name=data.get('name','');code=data.get('code','');notes=data.get('notes','');active=data.get('active',True)
            if not isinstance(name,str) or not 1<=len(name.strip())<=100 or not isinstance(code,str) or not 1<=len(code.strip())<=60 or not isinstance(notes,str) or len(notes)>2000 or type(active) is not bool:raise Problem('請填寫姓名與科室內唯一編號；備註最多 2000 字',422)
            if Person.objects.filter(department_id=p.department_id,code=code.strip()).exclude(id=p.id).exists():raise Problem('此科室內部編號已存在；同名人物請使用不同編號',409)
            p.name=name.strip();p.code=code.strip();p.notes=notes;p.active=active;p.version+=1;p.save();audit(u,'person_save',p.id,{'department':p.department_id,'version':p.version,'active':p.active})
            return JsonResponse({'person':person_data(p)})
    if path=='people/reference-upload':
        p=get_person(u,request.POST['person'],'people_manage')
        if not p.active:raise Problem('人物已停用',409)
        files=request.FILES.getlist('files')
        if not 1<=len(files)<=5:raise Problem('每批請上傳 1 至 5 張參考照片',422)
        results=[]
        for file in files:
            temporary=None;stored=None
            try:
                if file.size>10*1024*1024:raise Problem('每張參考照片最多 10 MB',422)
                try:
                    with Image.open(file) as image:
                        if image.width*image.height>40000000:raise Problem('參考照片超過 4000 萬像素',422)
                        normalized=ImageOps.exif_transpose(image).convert('RGB');normalized.thumbnail((1600,1600));out=io.BytesIO();normalized.save(out,'JPEG',quality=95);content=out.getvalue()
                except (OSError,ValueError,UnidentifiedImageError,Image.DecompressionBombError):raise Problem('無法解碼參考照片',422)
                sha=hashlib.sha256(content).hexdigest()
                temporary=default_storage.save('people/staging/'+str(uuid.uuid4())+'.jpg',ContentFile(content))
                result=extract([('reference',default_storage.path(temporary))])[0]
                faces=result.get('faces',[])
                if result.get('error'):raise Problem(result['error'],422)
                if len(faces)!=1 or not faces[0].get('quality',{}).get('usable') or not valid_vector(faces[0].get('vector')):raise Problem('請使用只有一張清晰人臉的照片；無人臉、多張人臉、過小或模糊均不納入',422)
                with transaction.atomic():
                    p=get_person(u,p.id,'people_manage',lock=True)
                    if not p.active:raise Problem('人物已停用',409)
                    if p.references.filter(active=True).count()>=20:raise Problem('每人最多 20 張啟用中的參考照片',422)
                    if p.references.filter(sha=sha,active=True).exists():raise Problem('此參考照片已存在',409)
                    face=faces[0];r=PersonReference(person=p,creator=u,sha=sha,vector=face['vector'],bbox=face['bbox'],quality=face['quality'],model_key=MODEL_KEY)
                    r.file.save(str(uuid.uuid4())+'.jpg',ContentFile(content),save=False);stored=r.file.name;r.save();p.version+=1;p.save(update_fields=['version'])
                    audit(u,'person_reference_add',r.id,{'person':str(p.id),'model':MODEL_KEY})
                stored=None;results.append({'name':file.name,'ok':True,'id':str(r.id)})
            except Problem as e:results.append({'name':file.name,'error':e.message})
            except ModelUnavailable as e:results.append({'name':file.name,'error':str(e)})
            finally:
                if temporary:default_storage.delete(temporary)
                if stored:default_storage.delete(stored)
        return JsonResponse({'results':results})
    if path=='people/reference-state':
        with transaction.atomic():
            p=get_person(u,data['person'],'people_manage',lock=True)
            if data.get('version')!=p.version:raise Problem('參考資料已更新，請重新載入',409)
            try:reference_id=uuid.UUID(str(data['reference']))
            except ValueError:raise Problem('參考照片識別格式錯誤',422)
            r=p.references.filter(id=reference_id).first()
            if not r:raise Problem('找不到參考照片',404)
            if type(data.get('active')) is not bool:raise Problem('狀態無效',422)
            if data['active'] and not r.active and p.references.filter(active=True).count()>=20:raise Problem('最多 20 張啟用參考照片',422)
            r.active=data['active'];r.save(update_fields=['active']);p.version+=1;p.save(update_fields=['version']);audit(u,'person_reference_state',r.id,{'active':r.active})
            return JsonResponse({'ok':True})
    if path in ['people/status','people/index']:
        p=get_person(u,data['person'],'face_search');q=scope(p)
        if not p.active:raise Problem('人物已停用',409)
        if path=='people/index':
            done=FacePhotoIndex.objects.filter(model_key=MODEL_KEY,source_sha=F('photo__sha'),photo_id__in=q.values('id'))
            if data.get('retry'):done.filter(state='failed').delete()
            rows=list(q.exclude(id__in=done.values('photo_id')).order_by('id')[:5])
            if rows:
                try:extracted={r['id']:r for r in extract([(p.id,p.file.path) for p in rows])}
                except ModelUnavailable as e:raise Problem(str(e),503)
                with transaction.atomic():
                    p=get_person(u,p.id,'face_search')
                    if not p.active:raise Problem('人物已停用，未發布此次索引',409)
                    current=scope(p)
                    for photo in rows:
                        if not current.filter(id=photo.id,sha=photo.sha).exists():continue
                        r=extracted.get(str(photo.id),{'error':'模型未回傳此照片'})
                        valid=[]
                        for ordinal,face in enumerate(r.get('faces',[])):
                            if face.get('quality',{}).get('usable') and valid_vector(face.get('vector')):valid.append((ordinal,face))
                        for ordinal,face in valid:FaceObservation.objects.update_or_create(photo=photo,source_sha=photo.sha,model_key=MODEL_KEY,ordinal=ordinal,defaults={'vector':face['vector'],'bbox':face['bbox'],'quality':face['quality']})
                        FacePhotoIndex.objects.update_or_create(photo=photo,model_key=MODEL_KEY,defaults={'source_sha':photo.sha,'state':'failed' if 'error' in r else 'ready','error':r.get('error','')})
                    audit(u,'face_index',p.department_id,{'photos':[x.id for x in rows],'model':MODEL_KEY})
        return JsonResponse({'ready':readiness(),'model':MODEL_KEY,'minimum':MINIMUM,**coverage(scope(p))})
    if path=='people/search':
        p=get_person(u,data['person'],'face_search')
        if not p.active:raise Problem('人物已停用',409)
        q=scope(p);refs=list(p.references.filter(active=True,model_key=MODEL_KEY))
        if not refs:raise Problem('此人物沒有適用目前模型的啟用參考照片',422)
        observations=FaceObservation.objects.filter(photo_id__in=q.values('id'),source_sha=F('photo__sha'),model_key=MODEL_KEY).select_related('photo__dataset__item')
        hits=[]
        for face in observations:
            if not valid_vector(face.vector):continue
            scored=[(sum(a*b for a,b in zip(face.vector,r.vector)),str(r.id)) for r in refs if valid_vector(r.vector)]
            if not scored:continue
            score,reference=max(scored)
            if score>=MINIMUM:hits.append((score,face,reference))
        hits.sort(key=lambda x:(-x[0],x[1].photo_id,x[1].ordinal));total=len(hits);results=[]
        # Fresh authorization and reference-version checks after scoring.
        fresh=get_person(u,p.id,'face_search')
        if fresh.version!=p.version or not fresh.active:raise Problem('人物參考資料已更新，請重新搜尋',409)
        current={x.id:x for x in scope(fresh).filter(id__in=[f.photo_id for _,f,_ in hits[:500]])}
        for score,face,reference in hits[:500]:
            photo=current.get(face.photo_id)
            if not photo:continue
            token=signing.dumps({'person':str(p.id),'person_version':p.version,'face':str(face.id),'sha':photo.sha,'photo_version':photo.version,'model':MODEL_KEY,'actor':u.id,'reference':reference},salt='face-candidate-v1')
            link=PersonPhotoLink.objects.filter(person=p,face=face,active=True).first()
            results.append({'photo':serialize(photo,u),'face':str(face.id),'bbox':face.bbox,'quality':face.quality,'ordinal':face.ordinal+1,'score':round(score,5),'reference':reference,'token':token,'linked':link.id if link else None,'can_confirm':allowed(u,p.department_id,'people_manage') and not photo.dataset.archived})
        audit(u,'face_search',p.id,{'model':MODEL_KEY,'returned':len(results)})
        return JsonResponse({'person':person_data(p),'candidates':results,'matches':total,'limited':total>500,'minimum':MINIMUM,**coverage(scope(p))})
    if path=='people/confirm':
        try:payload=signing.loads(data['token'],salt='face-candidate-v1',max_age=3600)
        except signing.BadSignature:raise Problem('候選已過期或無效，請重新搜尋',409)
        with transaction.atomic():
            p=get_person(u,payload['person'],'people_manage','face_search',lock=True)
            if payload['actor']!=u.id or payload['model']!=MODEL_KEY or p.version!=payload['person_version'] or not p.active:raise Problem('候選或人物資料已變動，請重新搜尋',409)
            face=FaceObservation.objects.select_related('photo__dataset__item').filter(id=payload['face'],model_key=MODEL_KEY).first()
            photo=scope(p).select_for_update().filter(id=face.photo_id).first() if face else None
            if not photo or photo.sha!=payload['sha'] or face.source_sha!=photo.sha or photo.version!=payload['photo_version'] or photo.dataset.archived:raise Problem('照片已變動、封存或無權查看，請重新搜尋',409)
            note=str(data.get('reason','')).strip()
            if not note or len(note)>2000:raise Problem('請填寫人工確認說明（最多 2000 字）',422)
            if PersonPhotoLink.objects.filter(face=face,active=True).exclude(person=p).exists():raise Problem('此人臉已有其他人物關聯，請先撤銷原關聯',409)
            link=PersonPhotoLink.objects.filter(person=p,face=face).first()
            if link and link.active:return JsonResponse({'ok':True,'link':link.id,'already':True})
            if link:link.active=True;link.version+=1;link.actor=u;link.save()
            else:link=PersonPhotoLink.objects.create(person=p,face=face,actor=u)
            PersonLinkEvent.objects.create(link=link,actor=u,action='confirmed',reason=note,detail={'reference':payload['reference'],'model':MODEL_KEY,'photo_version':photo.version})
            audit(u,'person_link_confirm',link.id,{'person':str(p.id),'photo':photo.id,'face':str(face.id)})
            return JsonResponse({'ok':True,'link':link.id})
    if path in ['people/links','people/unlink']:
        p=get_person(u,data['person'],*(['people_manage'] if path.endswith('unlink') else []))
        q=visible(u).filter(status='submitted',dataset__item__department_id=p.department_id)
        links=PersonPhotoLink.objects.filter(person=p,face__photo_id__in=q.values('id')).select_related('face__photo__dataset__item','actor')
        if path.endswith('unlink'):
            with transaction.atomic():
                link=links.select_for_update().filter(id=data['link']).first()
                if not link:raise Problem('找不到關聯或無權查看',404)
                if data.get('version')!=link.version:raise Problem('關聯已被修改，請重新載入',409)
                reason=str(data.get('reason','')).strip()
                if not reason or len(reason)>2000:raise Problem('撤銷關聯須填寫理由（最多 2000 字）',422)
                link.active=False;link.version+=1;link.save(update_fields=['active','version']);PersonLinkEvent.objects.create(link=link,actor=u,action='removed',reason=reason);audit(u,'person_link_remove',link.id)
            return JsonResponse({'ok':True})
        return JsonResponse({'links':[{'id':l.id,'version':l.version,'active':l.active,'photo':serialize(l.face.photo,u),'bbox':l.face.bbox,'stale':l.face.source_sha!=l.face.photo.sha or l.face.model_key!=MODEL_KEY,'can_remove':allowed(u,p.department_id,'people_manage'),'events':[{'actor':e.actor.username,'action':e.action,'reason':e.reason,'at':e.created.isoformat()} for e in l.events.select_related('actor').order_by('id')]} for l in links.order_by('-id')[:500]]})
    raise Problem('找不到人物功能',404)
